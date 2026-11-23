"""The pending change set: edits staged in `/data/pending` until an apply promotes them.

API edits never write to `/data/config`, with two exceptions: a password change and an allowlist
edit, which write `config.yaml` directly and are refused while edits are pending. Other edits stage
a whole file, or the deletion of one, here.
The pipeline reads `/data/config` overlaid with these edits, and calls `promote` only after
HAProxy runs the new release. A failed apply, a refused promote or a discard leaves `/data/config`
byte-identical.

Layout of `/data/pending` (0700, every file 0600, because a staged `config.yaml` holds the password
hash):

- `manifest.json` is the only source of truth. It holds the SHA-256 of each staged file as
  `/data/config` held it when that file was first staged (its base), the list of changes and
  whether a promote is under way.
- `files/<path>` holds the staged copy of each changed file, at the same relative path as in
  `/data/config`. A file that the manifest does not list as written is a leftover and is ignored.

Each operation writes the staged file first and the manifest last, so a crash leaves either the old
change set or the new one. A promote marks the manifest first, then writes each file into
`/data/config`, then removes the change set. If it stops part way, the next operation under the
lock finishes it. That recovery runs with no conflict check and no validation, on purpose: HAProxy
already runs the promoted config, so `/data/config` must catch up with it. Between the crash and
the next operation, `/data/config` can be mixed, with some files old and some new. Call `status`
at startup so the recovery runs early.

Hand edits of `/data/config` are allowed. If a staged file was added, removed or changed in
`/data/config` since its base, `status` reports a conflict and `promote` refuses to run, because
promoting would overwrite that hand edit. A hand edit of any other file is not a conflict: promote
never writes it, and the overlay holds it, so the apply validates it with the staged edits. A hand
edit made before a file's first change is part of its base, because the staged copy was built from
it.

Only `config.yaml`, `routes/<name>.yaml` and `includes/<name>.cfg` can be staged. `secrets.yaml` is
not covered: secrets are written directly by the store.

`migrate_files` does not migrate staged files. After an upgrade that migrates `/data/config`, a
staged file whose `/data/config` copy was migrated shows as a conflict, and a staged file at the old
version fails validation. Either way, the pending edits must be discarded.

This module only stages whole files and never edits a tree. A caller that edits a tree before it
stages it must handle YAML aliases, merge keys and comments itself.

Every public method takes the `HeldLock` from `Store.lock()`. Hold the lock from the read of a file
to the staging of its new content, so no other writer's edit is lost in between.
"""

from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from ruamel.yaml.comments import CommentedMap

from homeproxy.models import Config, Route, SafeError
from homeproxy.store.errors import StoreError, ValidationFailed
from homeproxy.store.files import Store, validate_config, validate_route
from homeproxy.store.lock import HeldLock
from homeproxy.store.pending_manifest import (
    CONFIG_FILE,
    ChangeKind,
    InvalidPendingPath,
    Manifest,
    PendingCorrupt,
    PendingManifest,
    check_pending_path,
    route_name,
)
from homeproxy.store.tree import dump_tree, parse_tree
from homeproxy.util.atomic import atomic_write, ensure_dir, fsync_dir

__all__ = [
    "CONFIG_FILE",
    "Change",
    "ChangeKind",
    "InvalidPendingPath",
    "Overlay",
    "PendingChanges",
    "PendingConflict",
    "PendingCorrupt",
    "PendingStatus",
    "config_files",
]


class PendingConflict(StoreError):
    """A staged file changed in `/data/config` since its base, so promoting would lose that change.
    The message names those files, relative to `/data/config`."""

    def __init__(self, paths: tuple[str, ...]) -> None:
        super().__init__(
            f"a file with pending edits was changed by hand ({', '.join(paths)}); "
            f"discard the pending edits to continue"
        )


@dataclass(frozen=True)
class Change:
    """One staged change. `path` is relative to `/data/config`, such as `routes/jellyfin.yaml`."""

    path: str
    kind: ChangeKind


@dataclass(frozen=True)
class PendingStatus:
    """The change set and its conflict state.

    `conflict` is true when a staged file was added, removed or changed in `/data/config` since its
    base, and `conflicting_paths` lists those files. Both are empty when nothing is pending.
    """

    changes: tuple[Change, ...]
    conflict: bool
    conflicting_paths: tuple[str, ...]


@dataclass(frozen=True)
class Overlay:
    """`/data/config` with the pending edits applied, validated file by file.

    Rules across files, such as the route name matching its file name for hand-made files, are the
    pipeline's job.
    """

    config: Config
    route_files: Mapping[str, Route]
    """Each route by the relative path of its file, such as `routes/app.yaml`."""
    includes: Mapping[str, bytes]
    files: Mapping[str, bytes]
    """Every file of the overlay by relative path. `config.yaml` holds the password hash, so never
    show these bytes without redaction."""

    @property
    def routes(self) -> tuple[Route, ...]:
        """The routes, in the order of their files."""
        return tuple(self.route_files.values())


def config_files(store: Store) -> dict[str, Path]:
    """Return the files of `/data/config` by relative path: `config.yaml`, `routes/*.yaml` and
    `includes/*.cfg`. Hidden files, such as `.tmp-*.tmp`, and anything else are left out.
    """
    config_dir = store.config_dir
    files: dict[str, Path] = {}
    if store.config_path.is_file():
        files[CONFIG_FILE] = store.config_path
    routes = [path for path in store.route_files() if path.is_file()]
    includes = sorted((config_dir / "includes").glob("*.cfg"))
    for path in routes + includes:
        if not path.name.startswith(".") and path.is_file():
            files[path.relative_to(config_dir).as_posix()] = path
    return files


class PendingChanges:
    """The pending change set of one store. See the module docstring for the rules."""

    def __init__(self, store: Store) -> None:
        """Keep the change set in `<store root>/pending`. Nothing is created until a change."""
        self._store = store
        self.root = store.root / "pending"
        self._manifest = PendingManifest(store, self.root)

    # Paths and validation

    def check_path(self, path: str) -> None:
        """Raise `InvalidPendingPath` or `InvalidRouteName` if `path` cannot be staged.

        The allowed paths are `config.yaml`, `routes/<route name>.yaml` and `includes/<name>.cfg`.
        """
        check_pending_path(self._store, path)

    def validate(self, path: str, data: bytes) -> None:
        """Validate a file as `stage` and `promote` do, so a caller staging several files can check
        them all first. Raises `ParseError` or `ValidationFailed`."""
        if path == CONFIG_FILE:
            validate_config(parse_tree(data, CONFIG_FILE), CONFIG_FILE)
            return
        name = route_name(path)
        if name is None:
            return
        file = f"{name}.yaml"
        route = validate_route(parse_tree(data, file), file)
        if route.name != name:
            error: SafeError = {
                "loc": ("name",),
                "type": "name_mismatch",
                "msg": "the route name must match its file name",
            }
            raise ValidationFailed(file, [error])

    # The manifest

    def _load(self) -> Manifest | None:
        """Read the manifest, and finish a promote that a crash interrupted."""
        manifest = self._manifest.read()
        if manifest is not None and manifest.promoting:
            self._write_config(manifest)
            self._manifest.clear()
            return None
        return manifest

    def _add_change(self, manifest: Manifest, path: str, kind: ChangeKind) -> None:
        """Record a change. A file's first change records its base, the file as it is now."""
        if path not in manifest.changes:
            digest = self._config_digest(path)
            if digest is None:
                manifest.base_files.pop(path, None)
            else:
                manifest.base_files[path] = digest
        manifest.changes[path] = kind

    # Reading /data/config and the overlay

    def _config_bytes(self, path: str) -> bytes | None:
        try:
            return (self._store.config_dir / path).read_bytes()
        except FileNotFoundError:
            return None

    def _config_digest(self, path: str) -> str | None:
        data = self._config_bytes(path)
        return None if data is None else sha256(data).hexdigest()

    def _overlay_bytes(self, path: str, manifest: Manifest | None) -> bytes | None:
        kind = None if manifest is None else manifest.changes.get(path)
        if kind == "delete":
            return None
        if kind == "write":
            return self._manifest.read_staged(path)
        return self._config_bytes(path)

    # Public operations

    def status(self, lock: HeldLock) -> PendingStatus:
        """Return the staged changes, sorted by path, and whether `/data/config` has a conflict."""
        lock.require(self._store.lock_path)
        manifest = self._load()
        if manifest is None:
            return PendingStatus((), False, ())
        changes = tuple(Change(path, kind) for path, kind in sorted(manifest.changes.items()))
        conflicting = self._conflicting_paths(manifest)
        return PendingStatus(changes, bool(conflicting), conflicting)

    def promote_started(self, lock: HeldLock) -> bool:
        """Return whether a crash cut a promote after it began, so `/data/config` has the edits.

        Read it before any call that finishes the promote, because that clears the mark.
        """
        lock.require(self._store.lock_path)
        with suppress(PendingCorrupt):
            manifest = self._manifest.read()
            return manifest is not None and manifest.promoting
        return False

    def _conflicting_paths(self, manifest: Manifest) -> tuple[str, ...]:
        return tuple(
            sorted(
                path
                for path in manifest.changes
                if self._config_digest(path) != manifest.base_files.get(path)
            )
        )

    def read(self, path: str, lock: HeldLock) -> bytes | None:
        """Return a file as the overlay holds it, or None if it is missing or staged for deletion.

        `path` is relative to `/data/config` and must be a path that can be staged.
        """
        lock.require(self._store.lock_path)
        self.check_path(path)
        return self._overlay_bytes(path, self._load())

    def read_tree(self, path: str, lock: HeldLock) -> CommentedMap:
        """Return a YAML file of the overlay as a round-trip tree, to edit and stage again.

        A missing or deleted file raises `FileNotFoundError`.
        """
        data = self.read(path, lock)
        if data is None:
            raise FileNotFoundError(path)
        return parse_tree(data, Path(path).name)

    def overlay_files(self, lock: HeldLock) -> dict[str, bytes]:
        """Return every file of the overlay, sorted by relative path.

        The overlay is `config.yaml`, `routes/*.yaml` and `includes/*.cfg` from `/data/config`,
        with staged files replacing or adding files and staged deletions removing them.
        """
        lock.require(self._store.lock_path)
        manifest = self._load()
        paths = set(config_files(self._store))
        if manifest is not None:
            paths |= manifest.changes.keys()
        files: dict[str, bytes] = {}
        for path in sorted(paths):
            data = self._overlay_bytes(path, manifest)
            if data is not None:
                files[path] = data
        return files

    def load_overlay(self, lock: HeldLock) -> Overlay:
        """Return the overlay validated file by file. Raises `ParseError` or `ValidationFailed`.

        A missing `config.yaml` raises `FileNotFoundError`.
        """
        files = self.overlay_files(lock)
        if CONFIG_FILE not in files:
            raise FileNotFoundError(CONFIG_FILE)
        config = validate_config(parse_tree(files[CONFIG_FILE], CONFIG_FILE), CONFIG_FILE)
        routes: dict[str, Route] = {}
        includes: dict[str, bytes] = {}
        for path, data in files.items():
            if path.startswith("routes/"):
                file = Path(path).name
                routes[path] = validate_route(parse_tree(data, file), file)
            elif path.startswith("includes/"):
                includes[path] = data
        return Overlay(config, routes, includes, files)

    def stage(self, path: str, data: bytes, lock: HeldLock) -> None:
        """Stage new content for a file, after validating it. Raises before any write on failure.

        `config.yaml` and routes must parse and validate, and a route's name must match its file
        name. Includes are not validated here; `haproxy -c` checks them. Content equal to the file
        in `/data/config` removes the change, and an empty change set is removed.
        """
        lock.require(self._store.lock_path)
        self.check_path(path)
        self.validate(path, data)
        manifest = self._load()
        unchanged = data == self._config_bytes(path)
        if manifest is None:
            if unchanged:
                return
            self._manifest.remove_leftovers()
            manifest = Manifest({})
        if unchanged:
            manifest.changes.pop(path, None)
        else:
            self._manifest.write_staged(path, data)
            self._add_change(manifest, path, "write")
        self._manifest.save(manifest)
        if unchanged:
            self._manifest.unstage(path)

    def stage_tree(self, path: str, tree: CommentedMap, lock: HeldLock) -> None:
        """Stage a YAML tree for a file. See `stage`; the dumped text is what gets validated."""
        self.stage(path, dump_tree(tree, Path(path).name), lock)

    def stage_delete(self, path: str, lock: HeldLock) -> None:
        """Stage the deletion of a file. A file that the overlay lacks raises `FileNotFoundError`.

        Deleting a file that exists only in the change set removes its change.
        """
        lock.require(self._store.lock_path)
        self.check_path(path)
        manifest = self._load()
        if self._overlay_bytes(path, manifest) is None:
            raise FileNotFoundError(path)
        if manifest is None:
            self._manifest.remove_leftovers()
            manifest = Manifest({})
        if self._config_bytes(path) is None:
            manifest.changes.pop(path, None)
        else:
            self._add_change(manifest, path, "delete")
        self._manifest.save(manifest)
        self._manifest.unstage(path)

    def discard(self, lock: HeldLock) -> None:
        """Remove every pending change. `/data/config` is not touched.

        A promote that a crash interrupted is finished instead, because HAProxy already runs it.
        A change set that cannot be read is removed, even one marked as promoting whose staged
        files are missing, so the UI always has a way out. `/data/config` can then stay mixed
        between the old and the new files.
        """
        lock.require(self._store.lock_path)
        with suppress(PendingCorrupt):
            self._load()
        self._manifest.clear()

    def promote(self, lock: HeldLock) -> tuple[str, ...]:
        """Write the pending changes into `/data/config`, and return the paths changed.

        Call it only after HAProxy runs a release built from the overlay. It raises
        `PendingConflict` if `/data/config` changed by hand, and `ParseError` or `ValidationFailed`
        if a staged file is invalid, both before any write to `/data/config`. If it stops part way,
        for example on a full disk, it raises `OSError`, and the next operation under the lock
        finishes it.
        """
        lock.require(self._store.lock_path)
        manifest = self._load()
        if manifest is None:
            return ()
        conflicting = self._conflicting_paths(manifest)
        if conflicting:
            raise PendingConflict(conflicting)
        for path, kind in manifest.changes.items():
            if kind == "write":
                self.validate(path, self._manifest.read_staged(path))
        manifest.promoting = True
        self._manifest.save(manifest)
        self._write_config(manifest)
        self._manifest.clear()
        return tuple(sorted(manifest.changes))

    def _write_config(self, manifest: Manifest) -> None:
        """Write every change into `/data/config`. Each step can run again with the same result."""
        config_dir = self._store.config_dir
        for path, kind in sorted(manifest.changes.items()):
            target = config_dir / path
            if kind == "write":
                data = self._manifest.read_staged(path)
                ensure_dir(target.parent)
                atomic_write(target, data)
            else:
                removed = False
                with suppress(FileNotFoundError):
                    target.unlink()
                    removed = True
                if removed:
                    fsync_dir(target.parent)
