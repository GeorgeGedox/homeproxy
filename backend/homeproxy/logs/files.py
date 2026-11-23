"""Stored logs: one directory per source under `/data/state/logs`, each rotated by size.

The API writes `access` and `haproxy` from the log collector. s6-log writes `api`, `dns` and
`spoa` from each service's output and rotates them itself (`image/scripts/service-log`): `current`
and up to four archives named by their TAI64N time, which sort oldest first.

A line the API cannot write, for example on a full disk, is lost. The first failure and the
recovery are logged, never each line, and the collector and HAProxy keep running.
"""

import logging
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Literal

from homeproxy.logs.parser import AccessLog
from homeproxy.util.atomic import ensure_dir

Source = Literal["access", "haproxy", "api", "dns", "spoa"]
SOURCES: tuple[Source, ...] = ("access", "haproxy", "api", "dns", "spoa")

LOGS_DIR = Path("state") / "logs"
"""The stored logs, relative to the data directory. `/data/state` is not backed up."""

ACCESS_MAX_BYTES = 10 * 1024 * 1024
HAPROXY_MAX_BYTES = 1024 * 1024
ROTATED_FILES = 4
"""Files kept besides the current one, for the API's files and s6-log's (`n4`) alike."""

_WRITTEN_BY_API: tuple[Source, ...] = ("access", "haproxy")
_CHUNK = 64 * 1024

logger = logging.getLogger(__name__)


class _FileHandler(RotatingFileHandler):
    """Rotate by size and report a failing file once, then once more when it works again."""

    def __init__(self, path: Path, max_bytes: int) -> None:
        super().__init__(path, maxBytes=max_bytes, backupCount=ROTATED_FILES, delay=True)
        self._path = path
        self._failing = False
        self._failed_now = False

    def emit(self, record: logging.LogRecord) -> None:
        self._failed_now = False
        if self.stream is None:
            try:
                # init-data creates it in the image. This covers a deleted or missing directory.
                ensure_dir(self._path.parent)
            except OSError:
                self.handleError(record)
                return
        super().emit(record)
        if self._failing and not self._failed_now:
            self._failing = False
            logger.info("writing %s again", self._path)

    def handleError(self, record: logging.LogRecord) -> None:
        self._failed_now = True
        if self.stream is not None:
            # A broken stream is reopened by the next line.
            self.stream.close()
            self.stream = None
        if not self._failing:
            self._failing = True
            logger.error("cannot write %s, its lines are lost: %s", self._path, sys.exc_info()[1])


def _file_logger(root: Path, source: Source, max_bytes: int) -> logging.Logger:
    # A private logger outside the global tree, so each app owns its handler. `Logger.handle` runs
    # the redaction hook of `homeproxy.util.redact`, so a registered secret never reaches the file.
    file_logger = logging.Logger(f"{__name__}.{source}", logging.INFO)
    handler = _FileHandler(root / LOGS_DIR / source / f"{source}.log", max_bytes)
    handler.setFormatter(logging.Formatter("%(message)s"))
    file_logger.addHandler(handler)
    return file_logger


class LogFiles:
    """Write the collector's lines to the `access` and `haproxy` files until `close()`."""

    def __init__(
        self,
        root: Path,
        *,
        access_max_bytes: int = ACCESS_MAX_BYTES,
    ) -> None:
        """Write below the data directory `root`, rotating each file past its maximum size."""
        self._access = _file_logger(root, "access", access_max_bytes)
        self._haproxy = _file_logger(root, "haproxy", HAPROXY_MAX_BYTES)

    def access(self, entry: AccessLog) -> None:
        """Append an access line as HAProxy logged it, after its syslog time."""
        self._access.info("%s %s", entry.logged_at.isoformat(), entry.line)

    def haproxy(self, logged_at: datetime, message: str) -> None:
        """Append one of HAProxy's other lines, such as a notice that a server went down."""
        self._haproxy.info("%s %s", logged_at.isoformat(), message.rstrip("\n"))

    def close(self) -> None:
        """Close both files."""
        for file_logger in (self._access, self._haproxy):
            for handler in file_logger.handlers:
                handler.close()


def source_files(root: Path, source: Source) -> list[Path]:
    """Return the files of a stored log, oldest first."""
    directory = root / LOGS_DIR / source
    if source in _WRITTEN_BY_API:
        names = [f"{source}.log.{index}" for index in range(ROTATED_FILES, 0, -1)]
        paths = [directory / name for name in [*names, f"{source}.log"]]
    else:
        paths = [*sorted(directory.glob("@*")), directory / "current"]
    return [path for path in paths if path.is_file()]


def summary(root: Path, source: Source) -> tuple[int, datetime | None]:
    """Return the total size of a stored log and when it last changed, or None if it is empty."""
    size, newest = 0, None
    for path in source_files(root, source):
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        size += stat.st_size
        newest = max(newest or 0.0, stat.st_mtime)
    return size, None if newest is None else datetime.fromtimestamp(newest, UTC)


def read_source(root: Path, source: Source) -> Iterator[bytes]:
    """Yield the bytes of a stored log, oldest file first.

    A file that rotation removes before it is opened is skipped, so a download that overlaps a
    rotation can miss or repeat the lines of that moment.
    """
    for path in source_files(root, source):
        try:
            with path.open("rb") as file:
                while chunk := file.read(_CHUNK):
                    yield chunk
        except FileNotFoundError:
            continue
