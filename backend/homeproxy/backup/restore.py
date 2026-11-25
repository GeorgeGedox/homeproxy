"""Check a backup, or restore it onto a fresh or a set-up install.

The caller makes a staging directory with `staging_dir`, writes the upload to `UPLOAD_NAME` in it,
and calls `check_backup` or `restore_backup`. The archive is extracted into the staging directory,
migrated and validated there. A check then describes the files and changes nothing. A restore
makes each top-level entry of `/data` the backup's: an entry the archive does not hold is left
empty, `pending` is emptied, and the staged `secrets.yaml` gets a new `session_key`, so every
session ends. A failure before the first rename leaves `/data` as it was. A failure during the
renames puts back the entries that moved.
"""

import logging
import os
import secrets
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from homeproxy.backup.errors import RestoreRefused
from homeproxy.backup.extract import extract_archive, not_a_backup
from homeproxy.backup.layout import INCLUDED
from homeproxy.backup.summary import BackupSummary, newest_member_time, summarize
from homeproxy.store import HeldLock, MigrationError, StoreError, migrate_files
from homeproxy.store.files import Store
from homeproxy.store.tree import load_tree
from homeproxy.util.atomic import ensure_dir, fsync_dir

logger = logging.getLogger(__name__)

UPLOAD_NAME = "upload.tar.gz"
_OLD = ".old"
_REPLACED = (*INCLUDED, "pending")
"""The entries a restore replaces. `pending` is the one that an archive never holds."""


@contextmanager
def staging_dir(root: Path) -> Iterator[Path]:
    """Make `root/.restore-<random>`, private, and remove it with everything in it at the end."""
    ensure_dir(root)
    path = root / f".restore-{secrets.token_hex(8)}"
    path.mkdir(mode=0o700)
    try:
        yield path
    finally:
        try:
            shutil.rmtree(path)
        except OSError as error:
            logger.warning("cannot remove %s: %s", path, error.strerror)


def _rewrite(staged: Store, held: HeldLock, listen: str | None) -> None:
    """Point `dashboard.listen` at `listen` when given, and make a new `session_key`."""
    # Late import: `auth.runtime` imports `backup.token`, which loads this module.
    from homeproxy.auth.runtime import new_session_key

    if listen is not None:
        config = load_tree(staged.config_path)
        config["dashboard"]["listen"] = listen
        staged.save_config(config, held)
    secrets_tree = load_tree(staged.secrets_path)
    secrets_tree["session_key"] = new_session_key()
    staged.save_secrets(secrets_tree, held)


def _validate(staged: Store, listen: str | None) -> tuple[int, bool]:
    """Migrate and validate the staged files, and rewrite them with `_rewrite`.

    Return the config version before migration, and whether a file was migrated.
    """
    if not (staged.config_path.is_file() and staged.secrets_path.is_file()):
        raise not_a_backup()
    refusal: RestoreRefused | None = None
    try:
        with staged.lock() as held:
            version = load_tree(staged.config_path).get("version")
            migrated = bool(migrate_files(staged, held))
            staged.load_config()
            staged.load_routes()
            staged.load_secrets()
            _rewrite(staged, held, listen)
    except MigrationError as error:
        refusal = RestoreRefused(422, "backup_unsupported_version", str(error))
    except StoreError as error:
        refusal = RestoreRefused(422, "backup_invalid_file", str(error))
    if refusal is not None:
        raise refusal
    return (version if isinstance(version, int) else 0), migrated


def _prepare(staging: Path, listen: str | None) -> tuple[Store, BackupSummary]:
    """Extract and validate the upload in `staging`. Return the staged store and its summary."""
    upload = staging / UPLOAD_NAME
    extract_archive(upload, staging)
    made_at = newest_member_time(upload)
    upload.unlink()
    staged = Store(staging)
    version, migrated = _validate(staged, listen)
    return staged, summarize(staged, made_at, version, migrated=migrated)


def _undo(root: Path, staging: Path, name: str) -> None:
    """Put one entry back where it was, as far as it moved. Failures are logged."""
    try:
        if not (staging / name).exists() and (root / name).exists():
            os.rename(root / name, staging / name)
        if (staging / _OLD / name).exists():
            os.rename(staging / _OLD / name, root / name)
    except OSError as error:
        logger.error("cannot put %s back after a failed restore: %s", name, error.strerror)


def _empty_where_missing(root: Path, staging: Path) -> None:
    """Stage an empty directory, with the mode of the old one, for each replaced entry that the
    archive lacks and `root` has, so that nothing of the old install stays."""
    for name in _REPLACED:
        if (root / name).is_dir() and not (staging / name).exists():
            (staging / name).mkdir(mode=0o700)
            (staging / name).chmod((root / name).stat().st_mode & 0o777)


def _swap(root: Path, staging: Path) -> None:
    """Rename each staged entry into `root`. The entry it replaces moves to `staging/.old`."""
    _empty_where_missing(root, staging)
    (staging / _OLD).mkdir(mode=0o700)
    tried: list[str] = []
    try:
        for name in _REPLACED:
            if not (staging / name).exists():
                continue
            tried.append(name)
            if (root / name).exists():
                os.rename(root / name, staging / _OLD / name)
            os.rename(staging / name, root / name)
        fsync_dir(root)
    except BaseException:
        for name in reversed(tried):
            _undo(root, staging, name)
        raise


def check_backup(staging: Path) -> BackupSummary:
    """Describe the archive at `staging/UPLOAD_NAME`. Nothing outside `staging` changes.

    Raises `RestoreRefused` as `restore_backup` does.
    """
    return _prepare(staging, None)[1]


def restore_backup(store: Store, staging: Path, listen: str) -> None:
    """Restore the archive at `staging/UPLOAD_NAME` into `store`'s data directory.

    `listen` becomes `dashboard.listen`. Every session ends, because the backup's `secrets.yaml`
    gets a new `session_key`. Raises `RestoreRefused` for an archive that is too large (413) or
    not valid (422), and `OSError` for a disk failure. Nothing in `store` changes then.
    """
    _prepare(staging, listen)
    with store.lock():
        _swap(store.root, staging)
