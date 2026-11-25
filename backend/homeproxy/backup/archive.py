"""Build the backup archive: a `tar.gz` with the layout of `/data`, limited to `INCLUDED`."""

import importlib.metadata
import tarfile
from datetime import UTC, datetime
from pathlib import Path

from homeproxy.backup.layout import INCLUDED
from homeproxy.store.files import Store
from homeproxy.store.lock import HeldLock


def archive_name(now: datetime | None = None) -> str:
    """Return the download file name, `homeproxy-backup-<version>-<UTC time>.tar.gz`."""
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    return f"homeproxy-backup-{importlib.metadata.version('homeproxy')}-{stamp}.tar.gz"


def _plain(member: tarfile.TarInfo) -> tarfile.TarInfo | None:
    """Keep only files and directories, and drop the owner, so the archive holds no host names."""
    if not (member.isreg() or member.isdir()):
        return None
    member.uid = member.gid = 0
    member.uname = member.gname = ""
    return member


def build_archive(store: Store, lock: HeldLock, destination: Path) -> None:
    """Write the archive of `store`'s data directory to `destination`.

    Hold the store lock, so no file changes during the copy. Raises `OSError` on a disk failure.
    """
    lock.require(store.lock_path)
    with tarfile.open(destination, "w:gz") as archive:
        for name in INCLUDED:
            if (store.root / name).is_dir():
                archive.add(store.root / name, arcname=name, filter=_plain)
