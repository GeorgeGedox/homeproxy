"""Snapshots: kept for rollback, restored into pending changes, never restoring the password."""

import os
import shutil
import stat
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest

from homeproxy.auth.passwords import hash_password, verify_password
from homeproxy.generator import render
from homeproxy.store import (
    LockNotHeld,
    MigrationError,
    Store,
    ValidationFailed,
    load_tree,
    write_tree,
)
from homeproxy.store import snapshots as snapshots_module
from homeproxy.store.lock import HeldLock
from homeproxy.store.migrate import Migrations
from homeproxy.store.pending import PendingChanges
from homeproxy.store.snapshots import KEEP, SnapshotNotFound, Snapshots
from homeproxy.util.atomic import atomic_write as real_write
from homeproxy.util.redact import register_secret

DATA = Path(__file__).parents[4] / "examples" / "data"
OLD_PASSWORD = "homeproxy-dev"  # noqa: S105
NEW_PASSWORD = "a-new-password"  # noqa: S105

NEW_ROUTE = b"""version: 1
name: blog
type: http
hosts: [blog.example.com]
exposure: lan
upstreams:
  - address: 192.168.1.10:8080
"""


@pytest.fixture
def store(tmp_path: Path) -> Store:
    shutil.copytree(DATA, tmp_path / "data")
    store = Store(tmp_path / "data")
    (store.config_dir / "includes").mkdir()
    (store.config_dir / "includes" / "extra.cfg").write_bytes(b"# extra\n")
    return store


@pytest.fixture
def held(store: Store) -> Iterator[HeldLock]:
    with store.lock() as lock:
        yield lock


@pytest.fixture
def snapshots(store: Store) -> Snapshots:
    return Snapshots(store)


@pytest.fixture
def pending(store: Store) -> PendingChanges:
    return PendingChanges(store)


def rendered(pending: PendingChanges, held: HeldLock) -> Mapping[str, str]:
    overlay = pending.load_overlay(held)
    includes = {Path(path).stem: data.decode() for path, data in overlay.includes.items()}
    basic_auth = Store(DATA).load_secrets().basic_auth
    return render(overlay.config, overlay.routes, includes, basic_auth).files


def change_everything(store: Store, pending: PendingChanges, held: HeldLock) -> None:
    """Edit, add and delete routes, delete the include, change a setting and the password."""
    tree = load_tree(store.route_path("jellyfin"))
    tree["exposure"] = "lan"
    pending.stage_tree("routes/jellyfin.yaml", tree, held)
    pending.stage("routes/blog.yaml", NEW_ROUTE, held)
    pending.stage_delete("routes/whoami.yaml", held)
    pending.stage_delete("includes/extra.cfg", held)
    config = load_tree(store.config_path)
    config["network"]["http_port"] = 8080
    config["dashboard"]["password_hash"] = hash_password(NEW_PASSWORD)
    config["dashboard"]["listen"] = "192.168.1.20:9443"
    config["dashboard"]["session_ttl"] = "8h"
    pending.stage_tree("config.yaml", config, held)
    pending.promote(held)


def test_restoring_an_old_snapshot_reproduces_its_generated_files(
    store: Store, snapshots: Snapshots, pending: PendingChanges, held: HeldLock
) -> None:
    before = rendered(pending, held)
    old = snapshots.create("first", held)
    change_everything(store, pending, held)
    snapshots.create("second", held)
    assert rendered(pending, held) != before

    snapshots.restore(old.id, held)

    assert rendered(pending, held) == before


def test_a_restore_keeps_the_current_password_listen_address_and_session_length(
    store: Store, snapshots: Snapshots, pending: PendingChanges, held: HeldLock
) -> None:
    old = snapshots.create("first", held)
    change_everything(store, pending, held)

    snapshots.restore(old.id, held)
    pending.promote(held)

    restored = store.load_config()
    password_hash = restored.dashboard.password_hash.get_secret_value()
    assert restored.network.http_port == 80
    assert verify_password(password_hash, NEW_PASSWORD)
    assert not verify_password(password_hash, OLD_PASSWORD)
    dashboard = restored.dashboard
    assert (dashboard.listen, dashboard.session_ttl) == ("192.168.1.20:9443", "8h")


def test_restoring_the_current_state_stages_nothing(
    store: Store, snapshots: Snapshots, pending: PendingChanges, held: HeldLock
) -> None:
    # A hand-written file in a style the store would rewrite, such as a leading `---` line.
    store.config_path.write_bytes(b"---\n" + store.config_path.read_bytes())
    latest = snapshots.create("first", held)

    snapshots.restore(latest.id, held)

    assert pending.status(held).changes == ()


def test_restore_fails_when_a_domain_credential_no_longer_exists(
    store: Store, snapshots: Snapshots, pending: PendingChanges, held: HeldLock
) -> None:
    """A domain with no certificate names no credentials, so it never fails this check."""
    config = load_tree(store.config_path)
    config["domains"][1] = {"zone": "example.net", "wildcard_cert": False}
    write_tree(store.config_path, config)
    old = snapshots.create("first", held)
    secrets = load_tree(store.secrets_path)
    del secrets["dns_credentials"]["cf-main"]
    store.save_secrets(secrets, held)

    with pytest.raises(ValidationFailed) as raised:
        snapshots.restore(old.id, held)

    assert [error["loc"] for error in raised.value.errors] == [("domains", 0, "credentials")]
    assert pending.status(held).changes == ()


def test_a_restore_works_while_a_hand_edit_breaks_the_live_config(
    store: Store, snapshots: Snapshots, pending: PendingChanges, held: HeldLock
) -> None:
    """Restore is the way back from a bad hand edit, so only the live dashboard section loads."""
    old = snapshots.create("first", held)
    config = load_tree(store.config_path)
    config["network"]["http_port"] = "eighty"
    write_tree(store.config_path, config)

    snapshots.restore(old.id, held)
    pending.promote(held)

    assert store.load_config().network.http_port == 80


def test_a_refused_restore_stages_nothing(
    store: Store, snapshots: Snapshots, pending: PendingChanges, held: HeldLock
) -> None:
    """Snapshots are not migrated, so a model made stricter by an upgrade can refuse a file."""
    old = snapshots.create("first", held)
    pending.stage("routes/blog.yaml", NEW_ROUTE, held)
    pending.promote(held)
    route = snapshots.root / old.id / "config" / "routes" / "whoami.yaml"
    route.write_bytes(route.read_bytes() + b"retired_field: true\n")

    with pytest.raises(ValidationFailed):
        snapshots.restore(old.id, held)

    assert pending.status(held).changes == ()


def test_diff_shows_what_a_restore_changes_and_never_the_password_hash(
    store: Store, snapshots: Snapshots, pending: PendingChanges, held: HeldLock
) -> None:
    include_secret = "snapshot-include-secret-value"  # noqa: S105
    register_secret(include_secret)
    (store.config_dir / "includes" / "extra.cfg").write_text(f"# token {include_secret}\n")
    old_hash = load_tree(store.config_path)["dashboard"]["password_hash"]
    old = snapshots.create("first", held)
    change_everything(store, pending, held)
    new_hash = load_tree(store.config_path)["dashboard"]["password_hash"]
    # A hash kept in a comment by a hand edit, never registered as a secret.
    comment_hash = "$argon2id$v=19$m=65536,t=3,p=4$c25hcHNob3Q$Y29tbWVudGVkLW91dA"
    config = store.config_path.read_text()
    store.config_path.write_text(f"# old: {comment_hash}\n{config}")

    diff = snapshots.diff(old.id)

    assert "-  http_port: 8080\n+  http_port: 80\n" in diff
    assert "--- /dev/null\n+++ b/includes/extra.cfg\n" in diff
    assert "--- a/routes/blog.yaml\n+++ /dev/null\n" in diff
    assert "password_hash" not in "".join(
        line for line in diff.splitlines() if line[:1] in "+-" and line[:3] not in ("+++", "---")
    )
    assert "-# old: [REDACTED]\n" in diff
    for hidden in (old_hash, new_hash, comment_hash, include_secret):
        assert hidden not in diff


def test_only_the_newest_snapshots_are_kept_newest_first(
    snapshots: Snapshots, held: HeldLock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Over 1000 flushes take about 10 s, and durability is not what this test checks.
    monkeypatch.setattr(os, "fsync", lambda descriptor: None)
    created = [snapshots.create(f"apply {number}", held) for number in range(KEEP + 2)]

    listed = snapshots.list()

    assert listed == created[::-1][:KEEP]
    assert sorted(path.name for path in snapshots.root.iterdir()) == [s.id for s in listed[::-1]]
    with pytest.raises(SnapshotNotFound):
        snapshots.get(created[0].id)


def test_snapshot_files_are_private_and_hold_no_secrets(
    snapshots: Snapshots, held: HeldLock
) -> None:
    snapshot = snapshots.create("first", held)

    paths = list((snapshots.root / snapshot.id).rglob("*"))

    assert "config/config.yaml" in [
        p.relative_to(snapshots.root / snapshot.id).as_posix() for p in paths
    ]
    assert not any(p.name == "secrets.yaml" for p in paths)
    for path in [snapshots.root, *paths]:
        assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600)


def test_a_snapshot_that_fails_part_way_is_never_listed(
    snapshots: Snapshots, held: HeldLock, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = snapshots.create("first", held)
    calls = 0

    def failing_write(path: Path, data: bytes, *, mode: int | None = None) -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("disk full")
        real_write(path, data, mode=mode)

    monkeypatch.setattr(snapshots_module, "atomic_write", failing_write)
    with pytest.raises(OSError, match="disk full"):
        snapshots.create("second", held)
    assert snapshots.list() == [first]

    monkeypatch.setattr(snapshots_module, "atomic_write", real_write)
    (snapshots.root / ".DS_Store").write_bytes(b"")
    third = snapshots.create("third", held)
    assert snapshots.list() == [third, first]
    assert sorted(path.name for path in snapshots.root.iterdir()) == [
        ".DS_Store",
        first.id,
        third.id,
    ]


def test_a_snapshot_from_before_changes_were_kept_reads_back_without_any(
    snapshots: Snapshots, held: HeldLock
) -> None:
    """Snapshots of an older version hold only a message, which every upgraded install has."""
    snapshot = snapshots.create("add routes/blog.yaml", held)
    (snapshots.root / snapshot.id / "meta.json").write_text('{"message": "add routes/blog.yaml"}')

    assert snapshots.get(snapshot.id).changes == ()


@pytest.mark.parametrize("snapshot_id", ["../config", "20260101T000000000000Z"])
def test_an_unknown_or_hostile_snapshot_id_is_not_found(
    snapshots: Snapshots, held: HeldLock, snapshot_id: str
) -> None:
    snapshots.create("first", held)

    for read in (snapshots.get, snapshots.diff):
        with pytest.raises(SnapshotNotFound):
            read(snapshot_id)
    with pytest.raises(SnapshotNotFound):
        snapshots.restore(snapshot_id, held)


def test_create_and_restore_need_the_live_lock(store: Store, snapshots: Snapshots) -> None:
    with store.lock() as held:
        snapshot = snapshots.create("first", held)
    with pytest.raises(LockNotHeld):
        snapshots.create("second", held)
    with pytest.raises(LockNotHeld):
        snapshots.restore(snapshot.id, held)


@pytest.mark.parametrize(
    ("before", "session_ttl", "taken"),
    [
        ("applied", "8h", True),
        # A password change writes `config.yaml` too, but snapshots never compare the hash.
        ("password changed", "8h", True),
        ("hand edit", "8h", False),
        ("applied", "12h", False),
    ],
    ids=["applied", "password-changed", "hand-edit", "no-change"],
)
def test_a_live_write_keeps_an_applied_config_applied_and_never_applies_a_hand_edit(
    store: Store, snapshots: Snapshots, held: HeldLock, before: str, session_ttl: str, taken: bool
) -> None:
    snapshots.create("apply", held)
    if before == "password changed":
        config = load_tree(store.config_path)
        config["dashboard"]["password_hash"] = hash_password(NEW_PASSWORD)
        write_tree(store.config_path, config)
    if before == "hand edit":
        store.route_path("blog").write_bytes(NEW_ROUTE)
    config = load_tree(store.config_path)
    config["dashboard"]["session_ttl"] = session_ttl

    snapshots.save_live_config(config, "edit dashboard.session_ttl", held)

    assert store.load_config().dashboard.session_ttl == session_ttl
    taken_messages = ["edit dashboard.session_ttl"] if taken else []
    assert [snapshot.message for snapshot in snapshots.list()] == [*taken_messages, "apply"]
    assert snapshots.is_applied() is (before != "hand edit")


def set_snapshot_version(snapshots: Snapshots, snapshot_id: str, version: int) -> None:
    for path in (snapshots.root / snapshot_id / "config").rglob("*.yaml"):
        raw = path.read_bytes().replace(b"version: 1\n", f"version: {version}\n".encode())
        path.write_bytes(raw)


def test_a_snapshot_from_an_older_version_is_migrated_when_it_is_restored(
    store: Store, snapshots: Snapshots, pending: PendingChanges, held: HeldLock
) -> None:
    old = snapshots.create("first", held)
    set_snapshot_version(snapshots, old.id, 0)
    steps: Migrations = {
        "config": {0: lambda _tree: None},
        "route": {0: lambda _tree: None},
        "secrets": {},
    }

    snapshots.restore(old.id, held, migrations=steps)
    pending.promote(held)

    assert store.load_config().version == 1
    assert {route.name for route in store.load_routes()} >= {"files", "whoami"}


def test_a_snapshot_from_a_newer_release_is_refused_and_stages_nothing(
    snapshots: Snapshots, pending: PendingChanges, held: HeldLock
) -> None:
    newer = snapshots.create("first", held)
    set_snapshot_version(snapshots, newer.id, 2)

    with pytest.raises(MigrationError, match="supports 1"):
        snapshots.restore(newer.id, held)

    assert pending.status(held).changes == ()
