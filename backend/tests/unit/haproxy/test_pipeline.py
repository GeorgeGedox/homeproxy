"""The apply pipeline before HAProxy: a change that fails validation never reaches it.

The paths that touch HAProxy are tested against a real one in `tests/integration/pipeline/`.
"""

import shutil
from pathlib import Path

import pytest
from passlib.hash import sha512_crypt

from homeproxy.haproxy import pipeline as pipeline_module
from homeproxy.haproxy.client import HAProxyClient
from homeproxy.haproxy.pipeline import Pipeline
from homeproxy.haproxy.release_links import GENERATED, PREVIOUS, RELEASES, link, live_release
from homeproxy.store import Store
from homeproxy.store import pending as pending_module
from homeproxy.store.pending import PendingChanges, config_files
from homeproxy.util.redact import REDACTED

DATA = Path(__file__).parents[4] / "examples" / "data"

DUPLICATE_ROUTE = b"""version: 1
name: copy
type: http
hosts: [jellyfin.example.com]
exposure: lan
upstreams:
  - address: 192.168.1.50:80
"""
NEW_ROUTE = DUPLICATE_ROUTE.replace(b"jellyfin", b"copy")


def config_bytes(store: Store) -> dict[str, bytes]:
    """The files of `/data/config`, to show that something left them unchanged."""
    return {path: file.read_bytes() for path, file in config_files(store).items()}


@pytest.fixture
def store(tmp_path: Path) -> Store:
    shutil.copytree(DATA, tmp_path / "data")
    return Store(tmp_path / "data")


@pytest.fixture
def pipeline(store: Store, tmp_path: Path) -> Pipeline:
    # No HAProxy listens here, so any call to it would fail the apply at a later stage.
    missing = str(tmp_path / "missing.sock")
    return Pipeline(store, client=HAProxyClient(missing, missing), haproxy_command=("false",))


def stage(store: Store, path: str, data: bytes) -> None:
    with store.lock() as held:
        PendingChanges(store).stage(path, data, held)


@pytest.mark.parametrize(
    ("path", "data", "error"),
    [
        (
            "routes/copy.yaml",
            DUPLICATE_ROUTE,
            "jellyfin.example.com is already served by another route",
        ),
        (
            "includes/bad.cfg",
            b"\xff\xfe",
            "includes/bad.cfg is not UTF-8 text",
        ),
        (
            "routes/copy.yaml",
            NEW_ROUTE.replace(b"hosts:", b"response_headers: {Upgrade: x}\nhosts:"),
            "route 'copy': response_headers: Upgrade cannot be set as a response header",
        ),
    ],
    ids=["cross-file-rule", "include-not-text", "generator-refuses"],
)
def test_an_invalid_change_stops_before_haproxy_and_stays_pending(
    store: Store, pipeline: Pipeline, path: str, data: bytes, error: str
) -> None:
    stage(store, path, data)
    before = config_bytes(store)

    result = pipeline.apply("admin")

    assert (result.success, result.stage, result.errors) == (False, "validate", (error,))
    assert config_bytes(store) == before
    with store.lock() as held:
        assert [change.path for change in PendingChanges(store).status(held).changes] == [path]
    [entry] = store.audit.read()
    assert (entry.user, entry.action, entry.details["stage"]) == ("admin", "apply", "validate")


def test_a_hand_edit_after_staging_stops_the_apply(store: Store, pipeline: Pipeline) -> None:
    stage(store, "routes/copy.yaml", NEW_ROUTE)
    store.route_path("copy").write_bytes(NEW_ROUTE)

    result = pipeline.apply("admin")

    assert result.stage == "validate"
    assert "changed by hand" in result.errors[0]


def test_recover_finishes_a_promote_cut_short(
    store: Store, pipeline: Pipeline, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage(store, "routes/copy.yaml", NEW_ROUTE)

    def crash(path: Path, data: bytes, *, mode: int | None = None) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(pending_module, "atomic_write", crash)
    with store.lock() as held, pytest.raises(OSError):
        PendingChanges(store).promote(held)
    monkeypatch.undo()

    pipeline.recover()

    assert store.route_path("copy").read_bytes() == NEW_ROUTE


def test_recover_drops_generated_prev_once_the_promote_began(
    store: Store, pipeline: Pipeline, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`/data/config` already holds the edits of the new release, so it must stay live."""
    for name in ("old", "new"):
        (store.root / "releases" / name).mkdir(parents=True)
    (store.root / "generated").symlink_to("releases/new")
    (store.root / "generated.prev").symlink_to("releases/old")
    stage(store, "routes/copy.yaml", NEW_ROUTE)

    def crash(path: Path, data: bytes, *, mode: int | None = None) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(pending_module, "atomic_write", crash)
    with store.lock() as held, pytest.raises(OSError):
        PendingChanges(store).promote(held)
    monkeypatch.undo()

    pipeline.recover()

    assert store.route_path("copy").read_bytes() == NEW_ROUTE
    assert (store.root / "generated").readlink() == Path("releases/new")
    assert not (store.root / "generated.prev").is_symlink()


def test_basic_auth_hashes_in_the_check_output_are_redacted(store: Store, tmp_path: Path) -> None:
    """`haproxy -c` can quote a userlist line, and its output reaches the API response."""
    # A hash this process has never seen, so only the apply's own load of secrets.yaml knows it.
    hashed = sha512_crypt.using(rounds=5000).hash(str(tmp_path))
    seeded = store.load_secrets().basic_auth["files"]["alice"].get_secret_value()
    store.secrets_path.write_text(store.secrets_path.read_text().replace(seeded, hashed))
    (store.root / "generated").symlink_to("releases/none")
    quote_userlists = ("sh", "-c", 'cat "$3/15-userlists.cfg"; exit 1', "sh")
    missing = str(tmp_path / "missing.sock")
    pipeline = Pipeline(store, HAProxyClient(missing, missing), haproxy_command=quote_userlists)

    result = pipeline.apply("admin")

    assert result.stage == "check"
    assert f"user alice password {REDACTED}" in result.haproxy_log
    assert hashed not in result.haproxy_log


@pytest.mark.parametrize("rollback_fails", [False, True], ids=["switch", "switch-and-rollback"])
def test_a_link_that_cannot_be_written_fails_the_apply_and_keeps_the_old_release(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rollback_fails: bool
) -> None:
    """A full or failing disk is the one cause. HAProxy was not touched, so it needs no reload.
    When the rollback cannot link either, `generated.prev` is left for the next start."""
    old = store.root / RELEASES / "old"
    old.mkdir(parents=True)
    link(store.root, old)
    stage(store, "routes/copy.yaml", NEW_ROUTE)

    def no_space(root: Path, release: Path, name: str = GENERATED) -> None:
        if name == GENERATED and (release != old or rollback_fails):
            raise OSError(28, "No space left on device")
        link(root, release, name)

    monkeypatch.setattr(pipeline_module, "link", no_space)
    missing = str(tmp_path / "missing.sock")
    pipeline = Pipeline(store, client=HAProxyClient(missing, missing), haproxy_command=("true",))

    result = pipeline.apply("admin")

    error = "[Errno 28] No space left on device"
    rollback = [f"rolling back also failed, the next start restores the old release: {error}"]
    assert (result.success, result.stage) == (False, "apply")
    assert result.errors == (
        f"cannot switch to the new release: {error}",
        *(rollback if rollback_fails else []),
    )
    assert live_release(store.root) == old
    assert (store.root / PREVIOUS).exists() is rollback_fails
    assert store.audit.read()[0].details["stage"] == "apply"
    with store.lock() as held:
        assert [change.path for change in PendingChanges(store).status(held).changes] == [
            "routes/copy.yaml"
        ]
