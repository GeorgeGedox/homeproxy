"""Pending edits: staged apart from `/data/config`, promoted whole, and conflicts are reported."""

import json
import shutil
import stat
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from homeproxy.models import HttpRoute
from homeproxy.store import (
    InvalidRouteName,
    LockNotHeld,
    ParseError,
    Store,
    ValidationFailed,
    load_tree,
)
from homeproxy.store import pending as pending_module
from homeproxy.store.lock import HeldLock
from homeproxy.store.pending import (
    Change,
    InvalidPendingPath,
    PendingChanges,
    PendingConflict,
    PendingCorrupt,
)
from homeproxy.store.tree import dump_tree
from homeproxy.util.atomic import atomic_write as real_write

DATA = Path(__file__).parents[4] / "examples" / "data"

NEW_ROUTE = b"""# A route that only exists in the change set.
version: 1
name: blog
type: http
hosts: [blog.example.com]
exposure: lan
upstreams:
  - address: 192.168.1.10:8080
"""

INCLUDE = b"# hand-written include\nbackend extra\n    server s1 192.168.1.10:9000\n"


@pytest.fixture
def store(tmp_path: Path) -> Store:
    shutil.copytree(DATA, tmp_path / "data")
    return Store(tmp_path / "data")


@pytest.fixture
def pending(store: Store) -> PendingChanges:
    return PendingChanges(store)


@pytest.fixture
def held(store: Store) -> Iterator[HeldLock]:
    with store.lock() as lock:
        yield lock


def files_under(path: Path) -> dict[str, bytes]:
    return {
        item.relative_to(path).as_posix(): item.read_bytes()
        for item in sorted(path.rglob("*"))
        if item.is_file()
    }


def edited_config(store: Store) -> bytes:
    return store.config_path.read_bytes().replace(b"session_ttl: 12h", b"session_ttl: 24h")


def edited_route(store: Store, name: str) -> bytes:
    tree = load_tree(store.route_path(name))
    tree["exposure"] = "lan"
    return dump_tree(tree, f"{name}.yaml")


def stage_one_of_each(store: Store, pending: PendingChanges, held: HeldLock) -> None:
    """Stage an edit, a new route, a deleted route and a new include."""
    pending.stage("config.yaml", edited_config(store), held)
    pending.stage("routes/blog.yaml", NEW_ROUTE, held)
    pending.stage("routes/jellyfin.yaml", edited_route(store, "jellyfin"), held)
    pending.stage_delete("routes/ssh.yaml", held)
    pending.stage("includes/extra.cfg", INCLUDE, held)


# Done when: a discarded or failed change leaves /data/config byte-identical.


def test_staging_writes_nothing_to_config(
    store: Store, pending: PendingChanges, held: HeldLock
) -> None:
    before = files_under(store.config_dir)
    stage_one_of_each(store, pending, held)
    assert files_under(store.config_dir) == before
    assert pending.status(held).changes == (
        Change("config.yaml", "write"),
        Change("includes/extra.cfg", "write"),
        Change("routes/blog.yaml", "write"),
        Change("routes/jellyfin.yaml", "write"),
        Change("routes/ssh.yaml", "delete"),
    )


def test_discard_leaves_config_byte_identical_and_removes_the_change_set(
    store: Store, pending: PendingChanges, held: HeldLock
) -> None:
    before = files_under(store.config_dir)
    stage_one_of_each(store, pending, held)
    pending.discard(held)
    assert files_under(store.config_dir) == before
    assert pending.status(held).changes == ()
    assert files_under(pending.root) == {}
    assert pending.overlay_files(held) == before


@pytest.mark.parametrize(
    ("path", "data", "error"),
    [
        ("config.yaml", b"version: 1\ndashboard: [\n", ParseError),
        ("config.yaml", b"version: 1\n", ValidationFailed),
        ("routes/jellyfin.yaml", b"version: 1\nname: jellyfin\ntype: nope\n", ValidationFailed),
        ("routes/blog.yaml", NEW_ROUTE.replace(b"name: blog", b"name: other"), ValidationFailed),
        ("routes/blog.yaml", b"- not a mapping\n", ParseError),
    ],
)
def test_an_invalid_stage_changes_nothing(
    store: Store,
    pending: PendingChanges,
    held: HeldLock,
    path: str,
    data: bytes,
    error: type[Exception],
) -> None:
    pending.stage("routes/jellyfin.yaml", edited_route(store, "jellyfin"), held)
    config_before = files_under(store.config_dir)
    pending_before = files_under(pending.root)
    with pytest.raises(error):
        pending.stage(path, data, held)
    assert files_under(store.config_dir) == config_before
    assert files_under(pending.root) == pending_before


def test_a_refused_promote_leaves_config_byte_identical(
    store: Store, pending: PendingChanges, held: HeldLock
) -> None:
    stage_one_of_each(store, pending, held)
    # A staged file broken after staging, by hand or by a newer release's rules.
    staged = pending.root / "files" / "routes" / "jellyfin.yaml"
    staged.write_bytes(staged.read_bytes().replace(b"type: http", b"type: nope"))
    before = files_under(store.config_dir)
    with pytest.raises(ValidationFailed):
        pending.promote(held)
    assert files_under(store.config_dir) == before
    assert len(pending.status(held).changes) == 5


def test_a_missing_staged_file_is_reported_and_promotes_nothing(
    store: Store, pending: PendingChanges, held: HeldLock
) -> None:
    stage_one_of_each(store, pending, held)
    (pending.root / "files" / "routes" / "blog.yaml").unlink()
    before = files_under(store.config_dir)
    with pytest.raises(PendingCorrupt):
        pending.promote(held)
    assert files_under(store.config_dir) == before


# Done when: a hand edit of a staged file is a conflict, and a hand edit of any other file is not.


def edit_config(store: Store) -> None:
    store.config_path.write_bytes(edited_config(store))


def remove_route(store: Store) -> None:
    store.route_path("jellyfin").unlink()


def add_route(store: Store) -> None:
    store.route_path("blog").write_bytes(NEW_ROUTE)


def add_include(store: Store) -> None:
    (store.config_dir / "includes").mkdir()
    (store.config_dir / "includes" / "extra.cfg").write_bytes(INCLUDE)


def add_comment(store: Store) -> None:
    path = store.route_path("ssh")
    path.write_bytes(path.read_bytes() + b"# a comment\n")


HAND_EDITS = pytest.mark.parametrize(
    ("edit", "path"),
    [
        (edit_config, "config.yaml"),
        (remove_route, "routes/jellyfin.yaml"),
        (add_route, "routes/blog.yaml"),
        (add_include, "includes/extra.cfg"),
        (add_comment, "routes/ssh.yaml"),
    ],
)


@HAND_EDITS
def test_a_hand_edit_of_a_staged_file_is_a_conflict(
    store: Store,
    pending: PendingChanges,
    held: HeldLock,
    edit: Callable[[Store], object],
    path: str,
) -> None:
    stage_one_of_each(store, pending, held)
    assert not pending.status(held).conflict
    edit(store)
    status = pending.status(held)
    assert status.conflict
    assert status.conflicting_paths == (path,)


@HAND_EDITS
def test_a_hand_edit_of_a_file_with_no_staged_change_is_not_a_conflict(
    store: Store,
    pending: PendingChanges,
    held: HeldLock,
    edit: Callable[[Store], object],
    path: str,
) -> None:
    """Promote never writes that file, and the overlay an apply validates holds the hand edit."""
    pending.stage("routes/git.yaml", edited_route(store, "git"), held)
    edit(store)
    assert not pending.status(held).conflict
    overlay = pending.overlay_files(held)
    assert overlay.get(path) == files_under(store.config_dir).get(path)
    assert pending.promote(held) == ("routes/git.yaml",)
    assert files_under(store.config_dir) == overlay


def test_a_conflict_blocks_promote_and_config_keeps_the_hand_edit(
    store: Store, pending: PendingChanges, held: HeldLock
) -> None:
    stage_one_of_each(store, pending, held)
    store.config_path.write_bytes(edited_config(store).replace(b"24h", b"48h"))
    before = files_under(store.config_dir)
    with pytest.raises(PendingConflict) as raised:
        pending.promote(held)
    assert "(config.yaml)" in str(raised.value)
    assert files_under(store.config_dir) == before
    assert len(pending.status(held).changes) == 5


def test_undoing_the_hand_edit_clears_the_conflict(
    store: Store, pending: PendingChanges, held: HeldLock
) -> None:
    original = store.config_path.read_bytes()
    pending.stage("config.yaml", edited_config(store), held)
    store.config_path.write_bytes(original + b"# edited by hand\n")
    assert pending.status(held).conflict
    store.config_path.write_bytes(original)
    assert not pending.status(held).conflict


def test_a_hand_edit_before_a_file_is_first_staged_is_part_of_its_base(
    store: Store, pending: PendingChanges, held: HeldLock
) -> None:
    """The dashboard builds the staged copy from the hand-edited file, so promote keeps the edit."""
    pending.stage("routes/blog.yaml", NEW_ROUTE, held)
    store.config_path.write_bytes(store.config_path.read_bytes() + b"# edited by hand\n")
    tree = pending.read_tree("config.yaml", held)
    tree["dashboard"]["session_ttl"] = "24h"
    pending.stage_tree("config.yaml", tree, held)
    assert not pending.status(held).conflict
    pending.promote(held)
    config = store.config_path.read_bytes()
    assert b"# edited by hand" in config
    assert b"session_ttl: 24h" in config


def test_nothing_pending_means_no_conflict(
    store: Store, pending: PendingChanges, held: HeldLock
) -> None:
    store.config_path.write_bytes(edited_config(store))
    status = pending.status(held)
    assert (status.changes, status.conflict, status.conflicting_paths) == ((), False, ())


@pytest.mark.parametrize(
    "name",
    [
        # What a crash in an atomic write leaves, beside config.yaml and in routes/.
        ".tmp-abc123.tmp",
        "routes/.tmp-abc123.tmp",
        # Hidden files that match a glob, such as an editor's.
        "routes/.x.yaml",
        "includes/.x.cfg",
        "routes/sub/x.yaml",
        "includes/x.cfg.orig",
    ],
)
def test_files_the_store_does_not_read_are_not_in_the_overlay(
    store: Store, pending: PendingChanges, held: HeldLock, name: str
) -> None:
    pending.stage("routes/blog.yaml", NEW_ROUTE, held)
    target = store.config_dir / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"anything")
    assert name not in pending.overlay_files(held)
    # A new route file next to them is.
    store.route_path("x").write_bytes(NEW_ROUTE.replace(b"name: blog", b"name: x"))
    assert "routes/x.yaml" in pending.overlay_files(held)


def test_staging_a_file_again_does_not_hide_a_hand_edit(
    store: Store, pending: PendingChanges, held: HeldLock
) -> None:
    pending.stage("config.yaml", edited_config(store), held)
    store.config_path.write_bytes(store.config_path.read_bytes() + b"# edited by hand\n")
    before = files_under(store.config_dir)
    pending.stage("config.yaml", edited_config(store).replace(b"24h", b"48h"), held)
    pending.stage("routes/jellyfin.yaml", edited_route(store, "jellyfin"), held)
    pending.stage_delete("routes/ssh.yaml", held)
    status = pending.status(held)
    assert status.conflict
    assert status.conflicting_paths == ("config.yaml",)
    with pytest.raises(PendingConflict):
        pending.promote(held)
    assert files_under(store.config_dir) == before


# Promote.


def test_promote_writes_exactly_the_overlay(
    store: Store, pending: PendingChanges, held: HeldLock
) -> None:
    stage_one_of_each(store, pending, held)
    overlay = pending.overlay_files(held)
    assert pending.promote(held) == (
        "config.yaml",
        "includes/extra.cfg",
        "routes/blog.yaml",
        "routes/jellyfin.yaml",
        "routes/ssh.yaml",
    )
    after = files_under(store.config_dir)
    assert after == overlay
    assert "routes/ssh.yaml" not in after
    assert pending.status(held).changes == ()
    assert files_under(pending.root) == {}
    assert "blog" in [route.name for route in store.load_routes()]


def test_promote_keeps_the_mode_of_an_existing_file(
    store: Store, pending: PendingChanges, held: HeldLock
) -> None:
    store.config_path.chmod(0o640)
    pending.stage("config.yaml", edited_config(store), held)
    pending.stage("routes/blog.yaml", NEW_ROUTE, held)
    pending.promote(held)
    assert stat.S_IMODE(store.config_path.stat().st_mode) == 0o640
    assert stat.S_IMODE(store.route_path("blog").stat().st_mode) == 0o600


def test_promote_with_nothing_pending_does_nothing(
    store: Store, pending: PendingChanges, held: HeldLock
) -> None:
    before = files_under(store.config_dir)
    assert pending.promote(held) == ()
    assert files_under(store.config_dir) == before


def test_a_promote_that_stops_part_way_is_finished_by_the_next_call(
    store: Store, pending: PendingChanges, held: HeldLock, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage_one_of_each(store, pending, held)
    overlay = pending.overlay_files(held)
    config_writes: list[Path] = []

    def failing_write(path: Path, data: bytes, *, mode: int | None = None) -> None:
        if store.config_dir in path.parents:
            config_writes.append(path)
            if len(config_writes) == 2:
                raise OSError(28, "No space left on device")
        real_write(path, data, mode=mode)

    monkeypatch.setattr(pending_module, "atomic_write", failing_write)
    with pytest.raises(OSError, match="No space"):
        pending.promote(held)
    monkeypatch.setattr(pending_module, "atomic_write", real_write)
    # Part of the change is in /data/config, which would look like a conflict.
    assert files_under(store.config_dir) != overlay

    status = pending.status(held)
    assert (status.changes, status.conflict) == ((), False)
    assert files_under(store.config_dir) == overlay


def test_a_crash_after_the_promote_mark_is_finished_by_discard(
    store: Store, pending: PendingChanges, held: HeldLock
) -> None:
    stage_one_of_each(store, pending, held)
    overlay = pending.overlay_files(held)
    manifest_path = pending.root / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["promoting"] = True
    manifest_path.write_text(json.dumps(manifest))
    # HAProxy already runs the new release, so discard must not drop it.
    pending.discard(held)
    assert files_under(store.config_dir) == overlay
    assert pending.status(held).changes == ()


def test_discard_removes_a_promoting_set_whose_staged_file_is_missing(
    store: Store, pending: PendingChanges, held: HeldLock
) -> None:
    stage_one_of_each(store, pending, held)
    manifest_path = pending.root / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["promoting"] = True
    manifest_path.write_text(json.dumps(manifest))
    (pending.root / "files" / "routes" / "blog.yaml").unlink()
    with pytest.raises(PendingCorrupt):
        pending.status(held)
    # The UI needs a way out of a set that can never be promoted.
    pending.discard(held)
    assert pending.status(held).changes == ()
    assert files_under(pending.root) == {}


# Staging rules.


def test_staging_the_current_content_removes_the_change(
    store: Store, pending: PendingChanges, held: HeldLock
) -> None:
    original = store.config_path.read_bytes()
    pending.stage("config.yaml", original, held)
    assert not (pending.root / "manifest.json").exists()
    pending.stage("config.yaml", edited_config(store), held)
    pending.stage("routes/blog.yaml", NEW_ROUTE, held)
    pending.stage("config.yaml", original, held)
    assert pending.status(held).changes == (Change("routes/blog.yaml", "write"),)
    assert not (pending.root / "files" / "config.yaml").exists()
    pending.stage_delete("routes/blog.yaml", held)
    assert pending.status(held).changes == ()
    assert files_under(pending.root) == {}


def test_staging_a_deleted_file_again_restores_it(
    store: Store, pending: PendingChanges, held: HeldLock
) -> None:
    original = store.route_path("ssh").read_bytes()
    pending.stage_delete("routes/ssh.yaml", held)
    assert pending.read("routes/ssh.yaml", held) is None
    with pytest.raises(FileNotFoundError):
        pending.stage_delete("routes/ssh.yaml", held)
    pending.stage("routes/ssh.yaml", original, held)
    assert pending.status(held).changes == ()


def test_deleting_a_missing_file_raises(pending: PendingChanges, held: HeldLock) -> None:
    with pytest.raises(FileNotFoundError):
        pending.stage_delete("routes/nothing.yaml", held)
    with pytest.raises(FileNotFoundError):
        pending.read_tree("routes/nothing.yaml", held)


def test_read_tree_edit_and_stage_keep_comments(
    store: Store, pending: PendingChanges, held: HeldLock
) -> None:
    pending.stage("routes/blog.yaml", NEW_ROUTE, held)
    tree = pending.read_tree("routes/blog.yaml", held)
    tree["exposure"] = "public"
    pending.stage_tree("routes/blog.yaml", tree, held)
    staged = pending.read("routes/blog.yaml", held)
    assert staged == NEW_ROUTE.replace(b"exposure: lan", b"exposure: public")


def test_load_overlay_validates_the_merged_view(
    store: Store, pending: PendingChanges, held: HeldLock
) -> None:
    stage_one_of_each(store, pending, held)
    overlay = pending.load_overlay(held)
    names = [route.name for route in overlay.routes]
    assert "blog" in names
    assert "ssh" not in names
    assert names == sorted(names)
    jellyfin = next(route for route in overlay.routes if route.name == "jellyfin")
    assert isinstance(jellyfin, HttpRoute)
    assert jellyfin.exposure == "lan"
    assert overlay.includes == {"includes/extra.cfg": INCLUDE}


def test_load_overlay_without_config_raises(
    store: Store, pending: PendingChanges, held: HeldLock
) -> None:
    pending.stage_delete("config.yaml", held)
    with pytest.raises(FileNotFoundError):
        pending.load_overlay(held)


@pytest.mark.parametrize(
    ("path", "error"),
    [
        ("secrets.yaml", InvalidPendingPath),
        ("../secrets/secrets.yaml", InvalidPendingPath),
        ("routes/../config.yaml", InvalidPendingPath),
        ("routes/...yaml", InvalidRouteName),
        ("routes/a/b.yaml", InvalidPendingPath),
        ("routes/Jelly.yaml", InvalidRouteName),
        ("routes/.yaml", InvalidRouteName),
        ("routes/jellyfin.yaml\n", InvalidPendingPath),
        ("routes/jelly\nfin.yaml", InvalidRouteName),
        ("/data/config/config.yaml", InvalidPendingPath),
        ("includes/../x.cfg", InvalidPendingPath),
        ("includes/.hidden.cfg", InvalidPendingPath),
        ("includes/x.cfg\n", InvalidPendingPath),
        ("includes/a/../../x.cfg", InvalidPendingPath),
        ("includes/x.yaml", InvalidPendingPath),
        ("config.yaml/", InvalidPendingPath),
    ],
)
def test_only_config_files_can_be_staged(
    store: Store, pending: PendingChanges, held: HeldLock, path: str, error: type[Exception]
) -> None:
    before = files_under(store.root)
    operations: list[Callable[[], object]] = [
        lambda: pending.stage(path, INCLUDE, held),
        lambda: pending.stage_delete(path, held),
        lambda: pending.read(path, held),
    ]
    for operation in operations:
        with pytest.raises(error) as raised:
            operation()
        assert path not in str(raised.value)
    assert files_under(store.root) == before


def test_every_operation_needs_the_live_lock(store: Store, pending: PendingChanges) -> None:
    with store.lock() as held:
        pass
    calls: list[Callable[[], object]] = [
        lambda: pending.status(held),
        lambda: pending.read("config.yaml", held),
        lambda: pending.overlay_files(held),
        lambda: pending.load_overlay(held),
        lambda: pending.stage("routes/blog.yaml", NEW_ROUTE, held),
        lambda: pending.stage_delete("routes/ssh.yaml", held),
        lambda: pending.discard(held),
        lambda: pending.promote(held),
    ]
    for call in calls:
        with pytest.raises(LockNotHeld):
            call()
    assert not pending.root.exists()


def test_pending_files_are_private(store: Store, pending: PendingChanges, held: HeldLock) -> None:
    stage_one_of_each(store, pending, held)
    assert stat.S_IMODE(pending.root.stat().st_mode) == 0o700
    for path in pending.root.rglob("*"):
        expected = 0o700 if path.is_dir() else 0o600
        assert stat.S_IMODE(path.stat().st_mode) == expected, path


def test_leftovers_from_a_crash_are_ignored_and_removed(
    store: Store, pending: PendingChanges, held: HeldLock
) -> None:
    # A crash after a staged file was written and before the manifest was.
    leftover = pending.root / "files" / "routes" / "blog.yaml"
    leftover.parent.mkdir(parents=True)
    leftover.write_bytes(NEW_ROUTE)
    assert pending.read("routes/blog.yaml", held) is None
    assert pending.status(held).changes == ()
    pending.stage("config.yaml", edited_config(store), held)
    assert not leftover.exists()
    assert pending.read("routes/blog.yaml", held) is None


@pytest.mark.parametrize(
    "manifest",
    [
        b"not json",
        b"[]",
        b'{"format": 2}',
        b'{"format": 1, "base_files": {"a": "x"}, "changes": {}, "promoting": false}',
        b'{"format": 1, "base_files": {}, '
        b'"changes": {"../../secrets/secrets.yaml": "write"}, "promoting": false}',
        b'{"format": 1, "base_files": {}, "changes": {"config.yaml": "move"}, "promoting": false}',
        b'{"format": 1, "base_files": {}, "changes": {}, "promoting": "yes"}',
    ],
)
def test_a_broken_manifest_is_reported_and_discard_removes_it(
    store: Store, pending: PendingChanges, held: HeldLock, manifest: bytes
) -> None:
    before = files_under(store.config_dir)
    pending.stage("config.yaml", edited_config(store), held)
    (pending.root / "manifest.json").write_bytes(manifest)
    with pytest.raises(PendingCorrupt):
        pending.status(held)
    with pytest.raises(PendingCorrupt):
        pending.promote(held)
    pending.discard(held)
    assert pending.status(held).changes == ()
    assert files_under(store.config_dir) == before
