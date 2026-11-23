"""The append-only audit log: one JSON object per line, rotated by size.

Entries are never changed or removed, except that rotation drops the oldest file. This is the one
store file that is not written by temp file and rename, because appending is its purpose. Each
append reaches the disk before `append` returns, so an entry survives a crash or a power cut.
"""

import json
import logging
import os
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from homeproxy.store.lock import file_lock
from homeproxy.util.atomic import ensure_dir, fsync_dir
from homeproxy.util.redact import redact_data

logger = logging.getLogger(__name__)

AUDIT_MAX_BYTES = 10 * 1024 * 1024
AUDIT_ROTATED_FILES = 5


@dataclass(frozen=True, slots=True)
class AuditEntry:
    """One recorded action. `details` holds action data such as a snapshot id."""

    time: str
    user: str
    action: str
    details: dict[str, object] = field(default_factory=dict)


class AuditLog:
    """The audit log in one directory, safe to use from several threads and processes."""

    def __init__(
        self,
        directory: Path,
        *,
        max_bytes: int = AUDIT_MAX_BYTES,
        rotated_files: int = AUDIT_ROTATED_FILES,
    ) -> None:
        """Rotate when an append would pass `max_bytes`, and keep `rotated_files` old files."""
        if rotated_files < 1:
            raise ValueError("rotated_files must be at least 1")
        self._directory = directory
        self._path = directory / "audit.jsonl"
        self._lock_path = directory / "audit.lock"
        self._max_bytes = max_bytes
        self._rotated_files = rotated_files

    def append(self, user: str, action: str, details: Mapping[str, object] | None = None) -> None:
        """Record an action now, with secrets redacted.

        Only secrets registered in this process are redacted, so keep unregistered ones out of
        `details`. Its values must be JSON-serializable, or this raises `TypeError` and records
        nothing. A disk failure raises `OSError`.
        """
        record = redact_data(
            {
                "time": datetime.now(UTC).isoformat(),
                "user": user,
                "action": action,
                "details": dict(details or {}),
            }
        )
        # ensure_ascii and the JSON escapes keep a value with a newline on one line.
        line = (json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n").encode()
        ensure_dir(self._directory)
        with file_lock(self._lock_path):
            self._rotate_if_needed(len(line))
            self._append_line(line)

    def record(self, user: str, action: str, details: Mapping[str, object] | None = None) -> None:
        """Append like `append`, for a change that is already done: a disk failure is only
        logged, because failing the request would not undo the change."""
        try:
            self.append(user, action, details)
        except OSError as error:
            logger.error("cannot write the audit log: %s", error)

    def read(self, limit: int = 100) -> list[AuditEntry]:
        """Return up to `limit` entries, newest first, across rotated files.

        A line that is not a valid entry, such as one torn by a crash, is skipped and counted in a
        log message. Secrets registered in this process are redacted again, because a value can
        become a registered secret after it was recorded.
        """
        if limit <= 0 or not self._directory.is_dir():
            return []
        entries: list[AuditEntry] = []
        skipped = 0
        with file_lock(self._lock_path):
            for line in self._lines_newest_first():
                entry = _parse_line(line)
                if entry is None:
                    skipped += 1
                    continue
                entries.append(entry)
                if len(entries) == limit:
                    break
        if skipped:
            logger.warning("skipped %d malformed lines in the audit log", skipped)
        return entries

    def _rotated(self, index: int) -> Path:
        return self._directory / f"{self._path.name}.{index}"

    def _lines_newest_first(self) -> Iterator[bytes]:
        paths = [self._path, *(self._rotated(i) for i in range(1, self._rotated_files + 1))]
        for path in paths:
            try:
                lines = path.read_bytes().splitlines()
            except FileNotFoundError:
                # Rotation that crashed part way can leave a gap in the numbers.
                continue
            yield from reversed(lines)

    def _rotate_if_needed(self, incoming: int) -> None:
        try:
            size = self._path.stat().st_size
        except FileNotFoundError:
            return
        if size == 0 or size + incoming <= self._max_bytes:
            return
        # Oldest first, so a crash leaves a gap in the numbers and loses nothing else.
        for index in range(self._rotated_files, 1, -1):
            older = self._rotated(index - 1)
            if older.exists():
                os.replace(older, self._rotated(index))
        os.replace(self._path, self._rotated(1))
        fsync_dir(self._directory)

    def _append_line(self, line: bytes) -> None:
        created = not self._path.exists()
        descriptor = os.open(self._path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            size = os.fstat(descriptor).st_size
            # A crash or a full disk can leave a torn line. Without this newline, the next entry
            # would join it and be lost with it.
            torn = size > 0 and os.pread(descriptor, 1, size - 1) != b"\n"
            remaining = memoryview(b"\n" + line if torn else line)
            while remaining:
                remaining = remaining[os.write(descriptor, remaining) :]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if created:
            fsync_dir(self._directory)


def _parse_line(line: bytes) -> AuditEntry | None:
    try:
        record = redact_data(json.loads(line))
    except (ValueError, RecursionError):
        return None
    if not isinstance(record, dict):
        return None
    time, user, action = record.get("time"), record.get("user"), record.get("action")
    details = record.get("details", {})
    if not (isinstance(time, str) and isinstance(user, str) and isinstance(action, str)):
        return None
    if not isinstance(details, dict):
        return None
    return AuditEntry(time=time, user=user, action=action, details=details)
