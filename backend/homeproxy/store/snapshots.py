"""Snapshots of `/data/config` in `/data/history`, for the History page and rollback.

A snapshot records a config that HAProxy runs: the Cloudflare reconcile runs only while
`/data/config` equals the newest one (`is_applied`). So the apply pipeline takes one after each
successful promote, and after an apply with nothing staged that made a hand edit live. A change that
is live at once, such as an allowlist edit, writes through `save_live_config`, which takes one only
if the config was applied before the write: an unapplied hand edit must stay unapplied.

A snapshot is a copy of the files `config_files` lists, so it never holds `secrets.yaml`. It does
hold `dashboard.password_hash`, so every directory is 0700 and every file 0600.

Layout: `history/<id>/config/` holds the files at their paths in `/data/config`, and
`history/<id>/meta.json` holds the message and the changes in plain words, which a snapshot taken
without them, such as an older one, reads back empty. The id is the UTC creation time, such as
`20261110T110600123456Z`, so ids sort by age. A snapshot is built under a dot-prefixed name and
renamed into place, and an old one is renamed to a dot-prefixed name before it is removed, so a
crash never leaves a partial snapshot in the list. Leftover `.tmp-` and `.del-` directories are
removed by the next `create`, and other files in `history` are left alone. Only the newest `KEEP`
snapshots are kept.

The password is never restored, and diffs never show it. `restore` puts the live password hash into
the staged `config.yaml`, and `diff` replaces the hash on both sides before comparing.
"""

import difflib
import json
import logging
import re
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from ruamel.yaml.comments import CommentedMap

from homeproxy.models import ChangeSummary, Config, SafeError
from homeproxy.models.config import Dashboard
from homeproxy.store.errors import StoreError, ValidationFailed
from homeproxy.store.files import Store, _checked, validate_config
from homeproxy.store.lock import HeldLock
from homeproxy.store.migrate import MIGRATIONS, Migrations, migrate_bytes
from homeproxy.store.pending import CONFIG_FILE, PendingChanges, config_files
from homeproxy.store.pending_manifest import route_name
from homeproxy.store.tree import dump_tree, load_tree, parse_tree
from homeproxy.util.atomic import atomic_write, ensure_dir, fsync_dir
from homeproxy.util.redact import REDACTED, redact

logger = logging.getLogger(__name__)

KEEP: Final = 50
"""The number of snapshots kept. `create` removes older ones."""

_ID_FORMAT: Final = "%Y%m%dT%H%M%S%fZ"
_ID: Final = re.compile(r"\d{8}T\d{12}Z")
_META: Final = "meta.json"
_ARGON2: Final = re.compile(r"\$argon2[^\s\"']+")


class SnapshotNotFound(StoreError):
    """No snapshot has this id. The id is not repeated, because it comes from a request."""

    def __init__(self) -> None:
        super().__init__("snapshot not found")


@dataclass(frozen=True)
class Snapshot:
    """One snapshot: its id, when it was taken, and the message and changes the pipeline gave it."""

    id: str
    created: datetime
    message: str
    changes: tuple[ChangeSummary, ...] = ()


def _without_password(data: bytes) -> bytes:
    """Return `config.yaml` with its password hash replaced, so a diff never shows it."""
    tree = parse_tree(data, CONFIG_FILE)
    dashboard = tree.get("dashboard")
    if isinstance(dashboard, dict) and "password_hash" in dashboard:
        dashboard["password_hash"] = REDACTED
    return dump_tree(tree, CONFIG_FILE)


def _diff_lines(path: str, data: bytes | None) -> list[str]:
    if data is None:
        return []
    if path != CONFIG_FILE:
        return data.decode(errors="replace").splitlines()
    text = _without_password(data).decode(errors="replace")
    # A hand edit can keep an old hash in a comment, which the tree edit does not reach.
    return _ARGON2.sub(REDACTED, text).splitlines()


def diff_files(before: Mapping[str, bytes], after: Mapping[str, bytes]) -> str:
    """Return a unified diff from one set of files to another, by relative path.

    The headers are `a/<path>` and `b/<path>`, with `/dev/null` for an added or removed file. The
    password hash in `config.yaml` is replaced on both sides, and registered secrets are redacted.
    An empty string means no difference.
    """
    parts: list[str] = []
    for path in sorted(before.keys() | after.keys()):
        lines = difflib.unified_diff(
            _diff_lines(path, before.get(path)),
            _diff_lines(path, after.get(path)),
            f"a/{path}" if path in before else "/dev/null",
            f"b/{path}" if path in after else "/dev/null",
            lineterm="",
        )
        parts.extend(lines)
    return redact("".join(f"{line}\n" for line in parts))


def _read_files(store: Store) -> dict[str, bytes]:
    return {path: file.read_bytes() for path, file in config_files(store).items()}


def _migrated(path: str, data: bytes, migrations: Migrations) -> bytes:
    if path == CONFIG_FILE:
        return migrate_bytes(data, "config", path, migrations=migrations)
    if route_name(path) is not None:
        return migrate_bytes(data, "route", path, migrations=migrations)
    return data


class Snapshots:
    """The snapshots of one store. See the module docstring for the rules."""

    def __init__(self, store: Store) -> None:
        """Keep the snapshots in `<store root>/history`. Nothing is created until `create`."""
        self._store = store
        self.root = store.root / "history"

    def create(
        self, message: str, lock: HeldLock, changes: Sequence[ChangeSummary] = ()
    ) -> Snapshot:
        """Copy `/data/config` into a new snapshot, remove the oldest beyond `KEEP`, return it."""
        lock.require(self._store.lock_path)
        created = datetime.now(UTC)
        snapshot_id = created.strftime(_ID_FORMAT)
        building = self.root / f".tmp-{snapshot_id}"
        ensure_dir(building)
        for path, data in _read_files(self._store).items():
            target = building / "config" / path
            ensure_dir(target.parent)
            atomic_write(target, data)
        meta = {"message": message, "changes": [change.model_dump() for change in changes]}
        atomic_write(building / _META, json.dumps(meta).encode())
        building.rename(self.root / snapshot_id)
        fsync_dir(self.root)
        self._prune()
        return Snapshot(snapshot_id, created, message, tuple(changes))

    def _ids(self) -> list[str]:
        """Return the ids of the snapshots, newest first."""
        if not self.root.is_dir():
            return []
        return sorted((p.name for p in self.root.iterdir() if _ID.fullmatch(p.name)), reverse=True)

    def _prune(self) -> None:
        for snapshot_id in self._ids()[KEEP:]:
            (self.root / snapshot_id).rename(self.root / f".del-{snapshot_id}")
        for prefix in (".tmp-", ".del-"):
            for leftover in self.root.glob(f"{prefix}*"):
                shutil.rmtree(leftover)

    def _dir(self, snapshot_id: str) -> Path:
        """Return the directory of a snapshot. An id that is not a snapshot id is not found."""
        directory = self.root / snapshot_id
        if not _ID.fullmatch(snapshot_id) or not directory.is_dir():
            raise SnapshotNotFound()
        return directory

    def get(self, snapshot_id: str) -> Snapshot:
        """Return one snapshot, or raise `SnapshotNotFound`."""
        meta = json.loads((self._dir(snapshot_id) / _META).read_bytes())
        created = datetime.strptime(snapshot_id, _ID_FORMAT).replace(tzinfo=UTC)
        changes = tuple(ChangeSummary.model_validate(item) for item in meta.get("changes", []))
        return Snapshot(snapshot_id, created, str(meta["message"]), changes)

    def list(self) -> list[Snapshot]:
        """Return every snapshot, newest first."""
        return [self.get(snapshot_id) for snapshot_id in self._ids()]

    def count(self) -> int:
        """Return how many snapshots there are, without reading them."""
        return len(self._ids())

    def is_applied(self) -> bool:
        """Return whether `/data/config` equals the newest snapshot, ignoring the password hash."""
        ids = self._ids()
        return bool(ids) and not self.diff(ids[0])

    def save_live_config(self, tree: CommentedMap, message: str, lock: HeldLock) -> None:
        """Write `config.yaml` for a change that is live at once, outside an apply.

        Take a snapshot if the write changed an applied config, so it stays applied. A failed
        snapshot is logged, and the write stands.
        """
        applied = self.is_applied()
        self._store.save_config(tree, lock)
        try:
            if applied and not self.is_applied():
                self.create(message, lock)
        except OSError as exc:
            logger.error("snapshot after a live config write failed: %s", exc)

    def _files(self, snapshot_id: str) -> dict[str, bytes]:
        # The snapshot has the same layout as a data directory, so the store's file rules apply.
        return _read_files(Store(self._dir(snapshot_id)))

    def diff(self, snapshot_id: str) -> str:
        """Return a unified diff from `/data/config` to the snapshot, which is what a restore
        would change. The password hash is replaced on both sides, and registered secrets are
        redacted. An empty string means no difference.
        """
        return diff_files(_read_files(self._store), self._files(snapshot_id))

    def restore(
        self, snapshot_id: str, lock: HeldLock, *, migrations: Migrations = MIGRATIONS
    ) -> None:
        """Stage the snapshot as pending changes, keeping the live password hash.

        The snapshot's files are migrated to this release's versions first, in memory. A snapshot
        from a newer release raises `MigrationError`.

        Afterwards the overlay holds exactly the snapshot's files: files it lacks are staged for
        deletion. Every file is checked, and the domains' credentials must exist in `secrets.yaml`,
        before anything is staged; a failure raises `ValidationFailed`.
        """
        lock.require(self._store.lock_path)
        files = {
            path: _migrated(path, data, migrations)
            for path, data in self._files(snapshot_id).items()
        }
        files[CONFIG_FILE] = self._config_to_restore(files[CONFIG_FILE])
        pending = PendingChanges(self._store)
        deleted = sorted(pending.overlay_files(lock).keys() - files.keys())
        # Check every file first, so a refused restore stages nothing.
        for path in deleted:
            pending.check_path(path)
        for path, data in files.items():
            pending.check_path(path)
            pending.validate(path, data)
        for path in deleted:
            pending.stage_delete(path, lock)
        for path, data in sorted(files.items()):
            pending.stage(path, data, lock)

    def _config_to_restore(self, data: bytes) -> bytes:
        """Return the snapshot's `config.yaml` with the live dashboard settings, after the checks.

        The password, the listen address and the session length change only from the dashboard's
        own endpoints. A restore that brought old ones back could lock the admin out, for example
        with an address the host no longer has.
        """
        tree = parse_tree(data, CONFIG_FILE)
        config = validate_config(tree, CONFIG_FILE)
        self._check_credentials(config)
        # Only the dashboard section: a hand edit elsewhere must not block the way back from it.
        live_tree = load_tree(self._store.config_path)
        live = _checked(CONFIG_FILE, Dashboard.model_validate, live_tree.get("dashboard"))
        kept = {
            "password_hash": live.password_hash.get_secret_value(),
            "listen": live.listen,
            "session_ttl": live.session_ttl,
        }
        old = config.dashboard
        restored = (old.password_hash.get_secret_value(), old.listen, old.session_ttl)
        if restored == tuple(kept.values()):
            return data
        tree["dashboard"].update(kept)
        return dump_tree(tree, CONFIG_FILE)

    def _check_credentials(self, config: Config) -> None:
        """Raise `ValidationFailed` if a domain names credentials missing from `secrets.yaml`."""
        if not config.domains:
            return
        known = self._store.load_secrets().dns_credentials
        errors: list[SafeError] = [
            {
                "loc": ("domains", index, "credentials"),
                "type": "missing_credentials",
                "msg": "these credentials no longer exist; add them before restoring",
            }
            for index, domain in enumerate(config.domains)
            if domain.credentials is not None and domain.credentials not in known
        ]
        if errors:
            raise ValidationFailed(CONFIG_FILE, errors)
