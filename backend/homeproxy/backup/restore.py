"""Restore a backup onto a fresh install.

The caller makes a staging directory with `staging_dir`, writes the upload to `UPLOAD_NAME` in it,
and calls `restore_backup`. The archive is extracted into the staging directory, migrated and
validated there, and only then does each top-level entry replace the one in `/data`. A failure
before the first rename leaves `/data` as it was. A failure during the renames puts back the
entries that moved.
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
from homeproxy.store import MigrationError, StoreError, migrate_files
from homeproxy.store.files import Store
from homeproxy.store.tree import load_tree
from homeproxy.util.atomic import ensure_dir, fsync_dir

logger = logging.getLogger(__name__)

UPLOAD_NAME = "upload.tar.gz"
_OLD = ".old"


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


def _validate(staged: Store, listen: str) -> None:
    """Migrate and validate the staged files, and point `dashboard.listen` at `listen`."""
    if not (staged.config_path.is_file() and staged.secrets_path.is_file()):
        raise not_a_backup()
    refusal: RestoreRefused | None = None
    try:
        with staged.lock() as held:
            migrate_files(staged, held)
            staged.load_config()
            staged.load_routes()
            staged.load_secrets()
            tree = load_tree(staged.config_path)
            tree["dashboard"]["listen"] = listen
            staged.save_config(tree, held)
    except MigrationError as error:
        refusal = RestoreRefused(422, "backup_unsupported_version", str(error))
    except StoreError as error:
        refusal = RestoreRefused(422, "backup_invalid_file", str(error))
    if refusal is not None:
        raise refusal


def _undo(root: Path, staging: Path, name: str) -> None:
    """Put one entry back where it was, as far as it moved. Failures are logged."""
    try:
        if not (staging / name).exists() and (root / name).exists():
            os.rename(root / name, staging / name)
        if (staging / _OLD / name).exists():
            os.rename(staging / _OLD / name, root / name)
    except OSError as error:
        logger.error("cannot put %s back after a failed restore: %s", name, error.strerror)


def _swap(root: Path, staging: Path) -> None:
    """Rename each staged entry into `root`. The entry it replaces moves to `staging/.old`."""
    (staging / _OLD).mkdir(mode=0o700)
    tried: list[str] = []
    try:
        for name in INCLUDED:
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


def restore_backup(store: Store, staging: Path, listen: str) -> None:
    """Restore the archive at `staging/UPLOAD_NAME` into `store`'s data directory.

    `listen` becomes `dashboard.listen`. Raises `RestoreRefused` for an archive that is too large
    (413) or not valid (422), and `OSError` for a disk failure. Nothing in `store` changes then.
    """
    upload = staging / UPLOAD_NAME
    extract_archive(upload, staging)
    upload.unlink()
    _validate(Store(staging), listen)
    with store.lock():
        _swap(store.root, staging)
