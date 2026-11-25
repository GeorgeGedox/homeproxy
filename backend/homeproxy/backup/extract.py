"""Read an uploaded archive into a staging directory. The archive is hostile input.

Only regular files and directories under a name of `INCLUDED` are taken. The code makes every
directory 0700 and every file 0600, whatever the archive says, and never overwrites a file. The
stdlib `data` filter runs on each member as a second check. The caps in `layout` stop an archive
that is large, or that has many members, before it fills the disk.
"""

import gzip
import os
import shutil
import tarfile
import zlib
from pathlib import Path

from homeproxy.backup.errors import RestoreRefused
from homeproxy.backup.layout import INCLUDED, MAX_EXTRACTED_BYTES, MAX_MEMBERS
from homeproxy.util.atomic import ensure_dir


def not_a_backup() -> RestoreRefused:
    """Return the refusal for a file that is not a HomeProxy backup."""
    return RestoreRefused(422, "not_a_backup", "The file is not a HomeProxy backup.")


def _refused(name: str, why: str) -> RestoreRefused:
    shown = name[:80] if name.isprintable() else "a member"
    return RestoreRefused(422, "backup_member_refused", f"{shown}: {why}")


def _parts(member: tarfile.TarInfo) -> list[str]:
    """Return the path parts of a member's name. Raises `RestoreRefused` unless the name is a
    relative path inside the archive with an allowed top-level entry. The root `./` gives `[]`."""
    name = member.name.removeprefix("./")
    if name in ("", "."):
        return []
    parts = name.split("/")
    if name.startswith("/") or any(part in ("", ".", "..") for part in parts) or "\0" in name:
        raise _refused(name, "the path is not allowed")
    if parts[0] not in INCLUDED:
        raise _refused(name, "this entry is not part of a backup")
    return parts


def _write_file(archive: tarfile.TarFile, member: tarfile.TarInfo, target: Path) -> None:
    source = archive.extractfile(member)
    if source is None:
        raise _refused(member.name, "the file cannot be read")
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with source, os.fdopen(descriptor, "wb") as handle:
        shutil.copyfileobj(source, handle)


def _extract_member(
    archive: tarfile.TarFile, member: tarfile.TarInfo, staging: Path, total: int
) -> int:
    """Write one member. Return `total` plus its size."""
    parts = _parts(member)
    if not parts and member.isdir():
        return total
    if not (member.isreg() or member.isdir()) or member.type == tarfile.GNUTYPE_SPARSE:
        raise _refused(member.name, "only files and directories are allowed")
    total += member.size
    if total > MAX_EXTRACTED_BYTES:
        raise RestoreRefused(413, "backup_too_large", "The backup holds too much data.")
    target = staging.joinpath(*parts)
    try:
        tarfile.data_filter(member, str(staging))
        if member.isdir():
            ensure_dir(target)
        else:
            ensure_dir(target.parent)
            _write_file(archive, member, target)
    except (FileExistsError, NotADirectoryError, IsADirectoryError):
        raise _refused(member.name, "the path is used twice") from None
    return total


def extract_archive(upload: Path, staging: Path) -> None:
    """Extract `upload` into the existing directory `staging`.

    Raises `RestoreRefused`: 413 over a cap, 422 for anything else wrong with the archive.
    """
    refusal: RestoreRefused | None = None
    try:
        total = 0
        with tarfile.open(upload, "r:gz") as archive:
            for count, member in enumerate(archive, start=1):
                if count > MAX_MEMBERS:
                    raise RestoreRefused(413, "backup_too_large", "The backup has too many files.")
                total = _extract_member(archive, member, staging, total)
    except RestoreRefused as error:
        refusal = error
    except (tarfile.TarError, EOFError, zlib.error, gzip.BadGzipFile):
        refusal = not_a_backup()
    if refusal is not None:
        raise refusal
