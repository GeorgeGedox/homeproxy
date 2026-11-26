"""Older files move forward, originals are backed up, and failure changes nothing."""

import secrets
import shutil
import traceback
from pathlib import Path
from typing import get_args

import pytest
from ruamel.yaml.comments import CommentedMap

from homeproxy.models.fields import ConfigVersion, RouteVersion, SecretsVersion
from homeproxy.store import (
    CURRENT_VERSIONS,
    MIGRATIONS,
    MigrationError,
    ParseError,
    Store,
    ValidationFailed,
    copy_for_upgrade,
    dump_tree,
    load_tree,
    migrate_files,
    migrate_tree,
    parse_tree,
    prune_backups,
)
from homeproxy.store.migrate import (
    KEEP_BACKUPS,
    STARTED_VERSION_FILE,
    FileKind,
    Migrations,
)
from homeproxy.util.atomic import atomic_write

DATA = Path(__file__).parents[4] / "examples" / "data"


def noop(_tree: CommentedMap) -> None:
    pass


def rename_session_ttl(tree: CommentedMap) -> None:
    """Move an old key to its new name, as a real migration step would."""
    tree["dashboard"]["session_ttl"] = tree["dashboard"].pop("session_length")


TO_V1: Migrations = {"config": {0: noop}, "route": {0: noop}, "secrets": {0: noop}}


@pytest.fixture
def store(tmp_path: Path) -> Store:
    shutil.copytree(DATA, tmp_path / "data")
    return Store(tmp_path / "data")


def set_version(path: Path, version: int) -> None:
    raw = path.read_bytes()
    assert b"version: 1\n" in raw
    path.write_bytes(raw.replace(b"version: 1\n", f"version: {version}\n".encode()))


def snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(item.relative_to(root)): item.read_bytes()
        for item in sorted(root.rglob("*"))
        if item.is_file()
    }


def backups_of(store: Store) -> list[Path]:
    if not store.migration_backups_dir.exists():
        return []
    return sorted(store.migration_backups_dir.iterdir())


@pytest.mark.parametrize(
    ("kind", "version_type"),
    [("config", ConfigVersion), ("route", RouteVersion), ("secrets", SecretsVersion)],
)
def test_current_version_is_the_one_the_models_accept(kind: FileKind, version_type: object) -> None:
    [literal] = get_args(get_args(version_type)[0])
    assert literal == CURRENT_VERSIONS[kind]


def test_registry_covers_every_file_kind() -> None:
    assert set(MIGRATIONS) == {"config", "route", "secrets"}


def test_current_files_are_never_rewritten(store: Store) -> None:
    before = snapshot(store.root)
    times = {path: path.stat().st_mtime_ns for path in store.root.rglob("*.yaml")}
    with store.lock() as held:
        assert migrate_files(store, held) == []
    assert snapshot(store.root) == before | {"store.lock": b""}
    assert {path: path.stat().st_mtime_ns for path in times} == times
    assert backups_of(store) == []


def test_missing_files_are_skipped(tmp_path: Path) -> None:
    store = Store(tmp_path)
    with store.lock() as held:
        assert migrate_files(store, held) == []


def test_migration_keeps_comments_and_key_order_and_sets_the_version(store: Store) -> None:
    original = store.config_path.read_bytes()
    set_version(store.config_path, 0)
    with store.lock() as held:
        changed = migrate_files(store, held, migrations=TO_V1)
    assert changed == [store.config_path]
    assert store.config_path.read_bytes() == original
    assert store.load_config().version == 1


def test_a_step_edits_the_tree_in_place(store: Store) -> None:
    raw = store.config_path.read_bytes()
    assert b"  session_ttl: 12h # keep\n" not in raw
    edited = raw.replace(b"version: 1\n", b"version: 0\n").replace(
        b"  session_ttl: 12h\n", b"  session_length: 12h # keep\n"
    )
    store.config_path.write_bytes(edited)
    migrations: Migrations = {"config": {0: rename_session_ttl}, "route": {}, "secrets": {}}
    with store.lock() as held:
        migrate_files(store, held, migrations=migrations)
    assert store.load_config().dashboard.session_ttl == "12h"
    assert b"# Seeded config for the dev stack" in store.config_path.read_bytes()


def test_original_bytes_are_backed_up_privately(store: Store) -> None:
    set_version(store.config_path, 0)
    set_version(store.route_path("jellyfin"), 0)
    set_version(store.secrets_path, 0)
    expected = {
        "config/config.yaml": store.config_path.read_bytes(),
        "config/routes/jellyfin.yaml": store.route_path("jellyfin").read_bytes(),
        "secrets/secrets.yaml": store.secrets_path.read_bytes(),
    }
    with store.lock() as held:
        changed = migrate_files(store, held, migrations=TO_V1)
    assert set(changed) == {store.config_path, store.route_path("jellyfin"), store.secrets_path}

    [backup] = backups_of(store)
    assert snapshot(backup) == expected
    assert store.migration_backups_dir.stat().st_mode & 0o777 == 0o700
    assert backup.stat().st_mode & 0o777 == 0o700
    for path in backup.rglob("*"):
        assert path.stat().st_mode & 0o777 == (0o600 if path.is_file() else 0o700)


def test_migrated_secrets_end_up_at_0600(store: Store) -> None:
    set_version(store.secrets_path, 0)
    store.secrets_path.chmod(0o644)
    with store.lock() as held:
        migrate_files(store, held, migrations=TO_V1)
    assert store.secrets_path.stat().st_mode & 0o777 == 0o600


def test_migrating_twice_keeps_both_sets_of_originals(store: Store) -> None:
    set_version(store.config_path, 0)
    with store.lock() as held:
        migrate_files(store, held, migrations=TO_V1)
        set_version(store.config_path, 0)
        migrate_files(store, held, migrations=TO_V1)
    assert len(backups_of(store)) == 2


def test_second_run_after_a_success_does_nothing(store: Store) -> None:
    set_version(store.config_path, 0)
    with store.lock() as held:
        assert migrate_files(store, held, migrations=TO_V1) == [store.config_path]
        assert migrate_files(store, held, migrations=TO_V1) == []
    assert len(backups_of(store)) == 1


def test_crash_after_the_first_file_is_finished_by_the_next_run(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_version(store.config_path, 0)
    set_version(store.secrets_path, 0)
    written: list[Path] = []

    def crash_on_second_real_file(path: Path, data: bytes, *, mode: int | None = None) -> None:
        if store.migration_backups_dir not in path.parents:
            if written:
                raise OSError("power cut")
            written.append(path)
        atomic_write(path, data, mode=mode)

    monkeypatch.setattr("homeproxy.store.migrate.atomic_write", crash_on_second_real_file)
    with store.lock() as held, pytest.raises(OSError, match="power cut"):
        migrate_files(store, held, migrations=TO_V1)
    monkeypatch.undo()

    assert written == [store.config_path]
    assert load_tree(store.secrets_path)["version"] == 0
    with store.lock() as held:
        assert migrate_files(store, held, migrations=TO_V1) == [store.secrets_path]
    assert store.load_secrets().version == 1


def test_bad_second_file_stops_the_run_before_any_write(store: Store) -> None:
    set_version(store.config_path, 0)
    store.route_path("jellyfin").write_bytes(b"version: [\n")
    before = snapshot(store.root)
    with store.lock() as held, pytest.raises(ParseError):
        migrate_files(store, held, migrations=TO_V1)
    assert snapshot(store.root) == before | {"store.lock": b""}


def test_result_that_fails_validation_changes_nothing(store: Store) -> None:
    set_version(store.config_path, 0)

    def breaks_it(tree: CommentedMap) -> None:
        tree["unknown_key"] = 1

    before = snapshot(store.root)
    migrations: Migrations = {"config": {0: breaks_it}, "route": {}, "secrets": {}}
    with store.lock() as held, pytest.raises(ValidationFailed):
        migrate_files(store, held, migrations=migrations)
    assert snapshot(store.root) == before | {"store.lock": b""}


def test_failed_step_does_not_show_its_error_or_the_value(store: Store) -> None:
    secret = "topsecret" + secrets.token_hex(16)
    set_version(store.secrets_path, 0)

    def raises_with_a_value(_tree: CommentedMap) -> None:
        raise KeyError(secret)

    before = store.secrets_path.read_bytes()
    migrations: Migrations = {"config": {}, "route": {}, "secrets": {0: raises_with_a_value}}
    with store.lock() as held, pytest.raises(MigrationError) as caught:
        migrate_files(store, held, migrations=migrations)
    error = caught.value
    assert error.__context__ is None
    assert error.__cause__ is None
    assert secret not in "".join(traceback.format_exception(error))
    assert "secrets.yaml" in str(error)
    assert store.secrets_path.read_bytes() == before


def test_migration_needs_the_lock(store: Store) -> None:
    with store.lock() as held:
        pass
    with pytest.raises(Exception, match="not held"):
        migrate_files(store, held)


def tree_at(version: str) -> CommentedMap:
    return parse_tree(f"# top\nversion: {version} # the version\nkey: old\n".encode(), "f")


def test_steps_run_in_order_up_to_the_target() -> None:
    def one(tree: CommentedMap) -> None:
        assert tree["version"] == 1
        tree["key"] = "two"

    def two(tree: CommentedMap) -> None:
        assert tree["version"] == 2
        tree["key"] += "-three"

    tree = tree_at("1")
    migrations: Migrations = {"config": {1: one, 2: two}, "route": {}, "secrets": {}}
    assert migrate_tree(tree, "config", "f", migrations=migrations, target=3)
    assert dump_tree(tree, "f") == b"# top\nversion: 3 # the version\nkey: two-three\n"


def test_tree_at_the_target_is_unchanged() -> None:
    tree = tree_at("1")
    assert not migrate_tree(tree, "config", "f")
    assert dump_tree(tree, "f") == dump_tree(tree_at("1"), "f")


def test_newer_file_is_refused_with_both_versions() -> None:
    with pytest.raises(MigrationError, match=r"version 2.*supports 1"):
        migrate_tree(tree_at("2"), "config", "f")


def test_version_without_a_step_is_refused() -> None:
    with pytest.raises(MigrationError, match="no migration"):
        migrate_tree(tree_at("0"), "config", "f")


@pytest.mark.parametrize(
    "version", ["true", "&a true", "1.0", '"1"', "null", "[1]", "{a: 1}", "0x1", "topsecret"]
)
def test_unusable_version_is_refused_without_repeating_it(version: str) -> None:
    with pytest.raises(MigrationError) as caught:
        migrate_tree(tree_at(version), "config", "f")
    assert "missing or invalid" in str(caught.value)
    assert "topsecret" not in str(caught.value)


def test_missing_version_is_refused() -> None:
    with pytest.raises(MigrationError, match="missing or invalid"):
        migrate_tree(parse_tree(b"key: value\n", "f"), "config", "f")


def test_a_step_of_one_kind_rewrites_only_the_files_of_that_kind(store: Store) -> None:
    routes = sorted(store.route_files())
    for route in routes:
        set_version(route, 0)
    config, secrets = store.config_path.read_bytes(), store.secrets_path.read_bytes()
    only_routes: Migrations = {"config": {}, "route": {0: noop}, "secrets": {}}
    with store.lock() as held:
        changed = migrate_files(store, held, migrations=only_routes)
    assert sorted(changed) == routes
    assert store.config_path.read_bytes() == config
    assert store.secrets_path.read_bytes() == secrets
    [backup] = backups_of(store)
    assert set(snapshot(backup)) == {f"config/routes/{route.name}" for route in routes}


def stamp_dirs(store: Store) -> list[str]:
    return [path.name for path in backups_of(store) if path.is_dir()]


def test_a_new_version_copies_config_and_secrets_and_a_restart_does_not(store: Store) -> None:
    with store.lock() as held:
        copied = copy_for_upgrade(store, held, "0.3.0")
        assert copied is not None
        assert copy_for_upgrade(store, held, "0.3.0") is None
        assert len(stamp_dirs(store)) == 1
        assert copy_for_upgrade(store, held, "0.4.0") is not None
    assert len(stamp_dirs(store)) == 2
    assert snapshot(copied) == {
        f"config/{name}": data for name, data in snapshot(store.config_dir).items()
    } | {f"secrets/{name}": data for name, data in snapshot(store.secrets_dir).items()}
    assert copied.stat().st_mode & 0o777 == 0o700
    assert (copied / "secrets" / "secrets.yaml").stat().st_mode & 0o777 == 0o600
    marker = store.migration_backups_dir / STARTED_VERSION_FILE
    assert marker.read_text().strip() == "0.4.0"


def test_a_first_start_with_no_files_records_the_version_and_copies_nothing(tmp_path: Path) -> None:
    store = Store(tmp_path)
    with store.lock() as held:
        assert copy_for_upgrade(store, held, "0.3.0") is None
        assert copy_for_upgrade(store, held, "0.3.0") is None
    assert stamp_dirs(store) == []
    assert (store.migration_backups_dir / STARTED_VERSION_FILE).read_text().strip() == "0.3.0"


def test_a_failed_copy_does_not_record_the_version(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(path: Path, data: bytes, *, mode: int | None = None) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("homeproxy.store.migrate.atomic_write", fail)
    with store.lock() as held, pytest.raises(OSError, match="disk full"):
        copy_for_upgrade(store, held, "0.3.0")
    monkeypatch.undo()
    with store.lock() as held:
        assert copy_for_upgrade(store, held, "0.3.0") is not None


def test_only_the_newest_backup_directories_are_kept(store: Store) -> None:
    names = [f"2026010{day}T000000000000Z" for day in range(1, 8)]
    for name in names:
        (store.migration_backups_dir / name).mkdir(parents=True)
    (store.migration_backups_dir / STARTED_VERSION_FILE).write_text("0.3.0\n")
    prune_backups(store)
    assert stamp_dirs(store) == names[-KEEP_BACKUPS:]
    assert (store.migration_backups_dir / STARTED_VERSION_FILE).exists()
