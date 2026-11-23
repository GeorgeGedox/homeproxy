"""Schema migrations for the files that carry `version:`.

Every file format is at version 1, so `MIGRATIONS` is empty. To change a format, raise
`CURRENT_VERSION` and `homeproxy.models.fields.FileVersion`, and add a step for each file kind that
changed and an empty-bodied step for each that did not. A step edits the round-trip tree in place to
move it one version up, so comments and key order survive. The runner sets `version`.

`migrate_files` does nothing to a file that is already current, so a start never rewrites a
hand-edited file. It migrates and validates everything in memory first, so a bad file stops the run
before any write. Then it writes a backup of each original, then each new file. A crash or a disk
failure part way leaves each file valid at one version or the other, so the files can be mixed
between versions until the next run finishes the rest.

Backups are under `migration-backups/<UTC time>/`, with the same relative paths as the data
directory. The backup directory is private and each file is 0600, because backups hold
`secrets.yaml`. They are never removed automatically.
"""

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal, NamedTuple

from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.scalarint import ScalarInt

from homeproxy.store.errors import MigrationError
from homeproxy.store.files import Store, validate_config, validate_route, validate_secrets
from homeproxy.store.lock import HeldLock
from homeproxy.store.tree import dump_tree, parse_tree
from homeproxy.util.atomic import atomic_write, ensure_dir

type FileKind = Literal["config", "route", "secrets"]
type Migration = Callable[[CommentedMap], None]
"""Edits a tree in place, from the version it is registered under to the next one."""
type Migrations = Mapping[FileKind, Mapping[int, Migration]]

CURRENT_VERSION: Final = 1

MIGRATIONS: Final[Migrations] = {"config": {}, "route": {}, "secrets": {}}


class _PendingFile(NamedTuple):
    path: Path
    original: bytes
    migrated: bytes
    mode: int | None


def file_version(tree: CommentedMap, file: str) -> int:
    """Return a tree's `version`, which must be a decimal integer. The value is not repeated."""
    version = tree.get("version")
    # An exact type check, because true and a hex integer are subclasses of int. ruamel loads `0`
    # and anchored integers as ScalarInt.
    if type(version) not in (int, ScalarInt):
        raise MigrationError(f"{file!r} has a missing or invalid version")
    return int(version)


def migrate_tree(
    tree: CommentedMap,
    kind: FileKind,
    file: str,
    *,
    migrations: Migrations = MIGRATIONS,
    target: int = CURRENT_VERSION,
) -> bool:
    """Bring a tree up to `target` in place, and return whether anything changed.

    A file newer than `target` raises `MigrationError`, because this release cannot read it. A
    failed step also raises it, with only the file and the version, because a step's own error can
    quote a value from the file.
    """
    version = file_version(tree, file)
    if version > target:
        raise MigrationError(f"{file!r} has version {version}, and this release supports {target}")
    start = version
    while version < target:
        step = migrations[kind].get(version)
        if step is None:
            raise MigrationError(f"{file!r} has version {version}, which has no migration")
        failed = False
        try:
            step(tree)
        except Exception:
            failed = True
        if failed:
            raise MigrationError(f"migrating {file!r} from version {version} failed")
        version += 1
        tree["version"] = version
    return version != start


def _plan(
    path: Path, kind: FileKind, validate: Callable[[object, str], object], migrations: Migrations
) -> _PendingFile | None:
    """Return the migrated content of a file, or None if the file is missing or current."""
    try:
        original = path.read_bytes()
    except FileNotFoundError:
        return None
    tree = parse_tree(original, path.name)
    if not migrate_tree(tree, kind, path.name, migrations=migrations):
        return None
    validate(tree, path.name)
    mode = 0o600 if kind == "secrets" else None
    return _PendingFile(path, original, dump_tree(tree, path.name), mode)


def migrate_files(
    store: Store, lock: HeldLock, *, migrations: Migrations = MIGRATIONS
) -> list[Path]:
    """Migrate `config.yaml`, every route and `secrets.yaml`, and return the files changed.

    Raises `ParseError`, `MigrationError` or `ValidationFailed` before any write. A disk failure
    while writing raises `OSError`. The files can then be left mixed, some at the new version and
    some at the old one, until the next run finishes them. `/data/pending` is not covered.
    """
    lock.require(store.lock_path)
    planned = [
        _plan(store.config_path, "config", validate_config, migrations),
        *(_plan(path, "route", validate_route, migrations) for path in store.route_files()),
        _plan(store.secrets_path, "secrets", validate_secrets, migrations),
    ]
    pending = [item for item in planned if item is not None]
    if not pending:
        return []

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    backups = store.migration_backups_dir / stamp
    for item in pending:
        backup = backups / item.path.relative_to(store.root)
        ensure_dir(backup.parent)
        atomic_write(backup, item.original, mode=0o600)
    for item in pending:
        atomic_write(item.path, item.migrated, mode=item.mode)
    return [item.path for item in pending]
