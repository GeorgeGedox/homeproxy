"""The files of `/data/pending`: `manifest.json` and the staged copies under `files/`.

The rules of the change set are in `homeproxy.store.pending`. This module reads, checks and writes
its files, and knows which paths can be staged.
"""

import json
import re
import shutil
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal, cast

from homeproxy.store.errors import StoreError
from homeproxy.store.files import Store
from homeproxy.util.atomic import atomic_write, ensure_dir, fsync_dir

type ChangeKind = Literal["write", "delete"]

CONFIG_FILE: Final = "config.yaml"
"""The relative path of `config.yaml` in `/data/config` and in the pending change set."""

_FORMAT: Final = 1

_ROUTE_PATH = re.compile(r"routes/(?P<name>[^/]*)\.yaml")
_INCLUDE_PATH = re.compile(r"includes/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\.cfg")
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")


class PendingCorrupt(StoreError):
    """The pending change set cannot be read. `discard` still removes it."""


class InvalidPendingPath(StoreError):
    """A path that cannot be staged. The path itself is not repeated, as it may come from a user."""

    def __init__(self) -> None:
        super().__init__("this path cannot be staged")


def _corrupt() -> PendingCorrupt:
    """Return the error for a change set that cannot be read."""
    return PendingCorrupt("the pending change set cannot be read; discard it to continue")


def route_name(path: str) -> str | None:
    """Return the route name in a route path, or None for any other path."""
    match = _ROUTE_PATH.fullmatch(path)
    return None if match is None else match["name"]


def check_pending_path(store: Store, path: str) -> None:
    """Raise `InvalidPendingPath` or `InvalidRouteName` if `path` cannot be staged.

    The allowed paths are `config.yaml`, `routes/<route name>.yaml` and `includes/<name>.cfg`.
    """
    if path == CONFIG_FILE or _INCLUDE_PATH.fullmatch(path):
        return
    name = route_name(path)
    if name is None:
        raise InvalidPendingPath()
    # Raises InvalidRouteName for a name that is not a valid route name.
    store.route_path(name)


@dataclass
class Manifest:
    """The base digest of each staged file, the changes and whether a promote is under way."""

    base_files: dict[str, str]
    changes: dict[str, ChangeKind] = field(default_factory=dict)
    promoting: bool = False

    def to_bytes(self) -> bytes:
        """Return the manifest as JSON."""
        data = {
            "format": _FORMAT,
            "base_files": self.base_files,
            "changes": self.changes,
            "promoting": self.promoting,
        }
        return json.dumps(data, indent=2, sort_keys=True).encode() + b"\n"


def _parse_changes(store: Store, changes: dict[str, object]) -> dict[str, ChangeKind] | None:
    result: dict[str, ChangeKind] = {}
    for path, kind in changes.items():
        if kind not in ("write", "delete"):
            return None
        try:
            check_pending_path(store, path)
        except StoreError:
            return None
        result[path] = kind
    return result


def _parse_manifest(store: Store, raw: bytes) -> Manifest | None:
    """Return the manifest in `raw`, or None if it is not a valid manifest."""
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("format") != _FORMAT:
        return None
    base_files = data.get("base_files")
    changes = data.get("changes")
    promoting = data.get("promoting")
    if (
        not isinstance(base_files, dict)
        or not isinstance(changes, dict)
        or not isinstance(promoting, bool)
    ):
        return None
    if not all(
        isinstance(digest, str) and _SHA256_HEX.fullmatch(digest) for digest in base_files.values()
    ):
        return None
    result = _parse_changes(store, changes)
    if result is None:
        return None
    return Manifest(cast(dict[str, str], base_files), result, promoting)


class PendingManifest:
    """The manifest and staged copies in one `/data/pending` directory. Every file is 0600."""

    def __init__(self, store: Store, root: Path) -> None:
        """Keep the change set in `root`. Nothing is created until a save."""
        self._store = store
        self._root = root
        self._manifest_path = root / "manifest.json"
        self._files_dir = root / "files"

    def read(self) -> Manifest | None:
        """Read the manifest as it is, or None if there is none. Raises `PendingCorrupt`."""
        try:
            raw = self._manifest_path.read_bytes()
        except FileNotFoundError:
            return None
        manifest = _parse_manifest(self._store, raw)
        if manifest is None:
            raise _corrupt()
        return manifest

    def save(self, manifest: Manifest) -> None:
        """Write the manifest, or remove the change set if it holds no change."""
        if not manifest.changes:
            self.clear()
            return
        ensure_dir(self._root)
        atomic_write(self._manifest_path, manifest.to_bytes(), mode=0o600)

    def clear(self) -> None:
        """Remove the change set: the manifest first, which makes the staged files leftovers."""
        removed = False
        with suppress(FileNotFoundError):
            self._manifest_path.unlink()
            removed = True
        if removed:
            fsync_dir(self._root)
        self.remove_leftovers()

    def remove_leftovers(self) -> None:
        """Remove every staged copy. Call it only when no manifest lists them."""
        shutil.rmtree(self._files_dir, ignore_errors=True)

    def read_staged(self, path: str) -> bytes:
        """Return the staged copy of `path`. A missing copy raises `PendingCorrupt`."""
        try:
            return (self._files_dir / path).read_bytes()
        except FileNotFoundError:
            pass
        raise _corrupt()

    def write_staged(self, path: str, data: bytes) -> None:
        """Write the staged copy of `path`. Save the manifest after it."""
        staged = self._files_dir / path
        ensure_dir(staged.parent)
        atomic_write(staged, data, mode=0o600)

    def unstage(self, path: str) -> None:
        """Remove a staged copy that the manifest no longer lists. A leftover is harmless."""
        with suppress(FileNotFoundError):
            (self._files_dir / path).unlink()
