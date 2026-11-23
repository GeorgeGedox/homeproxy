"""Atomic file writes: temp file in the same directory, `fsync`, rename, `fsync` the directory.

A reader sees the old file or the new file, never part of one, and a crash at any point leaves one
of the two in place. A crash can leave a `.tmp-*.tmp` file behind. That name matches no glob the
project uses for its data files, such as `routes/*.yaml`. It also starts with a dot, because HAProxy
loads every other file in a `crt` directory and could not start with a leftover in `/data/certs/`.
"""

import os
import stat
import tempfile
from contextlib import suppress
from pathlib import Path

_DEFAULT_MODE = 0o600


def fsync_dir(path: Path) -> None:
    """Flush a directory, so a rename or a new file in it survives a power cut."""
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def ensure_dir(path: Path, mode: int = 0o700) -> None:
    """Create a directory and any missing parents durably, each with `mode` minus the umask."""
    if path.is_dir():
        return
    ensure_dir(path.parent, mode)
    # Another process can create it between the check and here.
    with suppress(FileExistsError):
        path.mkdir(mode=mode)
    fsync_dir(path.parent)


def _existing_mode(path: Path) -> int:
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        return _DEFAULT_MODE


def atomic_write(path: Path, data: bytes, *, mode: int | None = None) -> None:
    """Replace `path` with `data`. The parent directory must exist.

    `mode` is applied exactly, without the umask. With `mode=None` an existing file keeps its mode
    and a new file gets 0600, so a caller that forgets the mode exposes nothing. The temp file is
    never readable by others before its final mode is set.

    If `path` is a symlink, the link itself is replaced. On failure the old file is unchanged and
    the temp file is removed. A failure of the final directory flush raises after the new file is
    already in place.
    """
    final_mode = _existing_mode(path) if mode is None else mode
    descriptor, temp_name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), final_mode)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        with suppress(OSError):
            os.unlink(temp_name)
        raise
    fsync_dir(path.parent)
