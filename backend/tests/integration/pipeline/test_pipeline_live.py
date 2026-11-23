"""The apply pipeline against a real HAProxy, started as the s6 service starts it.

Each test gets a container with a temp data directory mounted at `/data`, and at its own host path
so that `docker exec <container> haproxy -c -f <host path>` checks exactly what the pipeline
wrote. The runtime sockets appear in a mounted directory, and the test reaches HAProxy's ports on
the container's own address, so no host port is bound. This needs Docker on the same kernel.

The fixtures live in this file because `mypy` treats a second `conftest.py` as a duplicate.
"""

import os
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path

import httpx2
import pytest

from homeproxy.haproxy.client import HAProxyClient
from homeproxy.haproxy.pipeline import ApplyResult, Pipeline
from homeproxy.haproxy.stats import StatRow
from homeproxy.models import ChangeSummary
from homeproxy.store import Store, load_tree
from homeproxy.store import pending as pending_module
from homeproxy.store.pending import PendingChanges, config_files
from homeproxy.store.snapshots import Snapshots
from tests.integration.haproxy_live import docker, haproxy_image, wait_for_haproxy

REPO_ROOT = Path(__file__).resolve().parents[4]
SEED = REPO_ROOT / "examples" / "data"
STARTUP_TIMEOUT = 30.0
HTTP_PORT = 8080
HTTPS_PORT = 8443
HOST = "app.example.com"

PLACEHOLDER = f"""\
global
    stats socket /run/haproxy/admin.sock mode 600 level admin expose-fd listeners

defaults
    mode http
    timeout connect 5s
    timeout client 30s
    timeout server 30s

frontend placeholder
    bind :{HTTP_PORT}
    http-request return status 503
"""

# HAProxy serves the test upstream itself, so no second container is needed. The CRLF line ends,
# as an editor on Windows writes them, must not make a release differ from itself.
UPSTREAM = b"""\
listen test_upstream
    bind 127.0.0.1:9000
    http-request return status 200 content-type text/plain string ok

listen other_upstream
    bind 127.0.0.1:9001
    http-request return status 200 content-type text/plain string other
""".replace(b"\n", b"\r\n")

# The seed's hash of the dev password.
PASSWORD_HASH = load_tree(SEED / "config" / "config.yaml")["dashboard"]["password_hash"]

CONFIG = f"""\
version: 1
dashboard:
  listen: 192.168.1.10:9443
  password_hash: "{PASSWORD_HASH}"
network:
  lan_cidrs: [192.168.1.0/24]
  haproxy_lan_ip: 192.168.1.10
  http_port: {HTTP_PORT}
  https_port: {HTTPS_PORT}
domains:
  - zone: example.com
    dns_provider: cloudflare
    credentials: cf-main
dns_server:
  listen: 192.168.1.10:5335
"""


def config_bytes(store: Store) -> dict[str, bytes]:
    """The files of `/data/config`, to show that something left them unchanged."""
    return {path: file.read_bytes() for path, file in config_files(store).items()}


def route(
    name: str,
    exposure: str = "public",
    upstream: str = "127.0.0.1:9000",
    extra_hosts: str = "",
) -> bytes:
    return (
        f"version: 1\nname: {name}\ntype: http\nhosts: [{name}.example.com{extra_hosts}]\n"
        f"exposure: {exposure}\nupstreams:\n  - address: {upstream}\n"
    ).encode()


@dataclass
class Live:
    """A running HAProxy, a store on its data directory and a pipeline between them."""

    store: Store
    address: str
    pipeline: Pipeline

    @property
    def client(self) -> HAProxyClient:
        return self.pipeline.client

    def stage(self, path: str, data: bytes) -> None:
        with self.store.lock() as held:
            PendingChanges(self.store).stage(path, data, held)

    def pending_paths(self) -> list[str]:
        with self.store.lock() as held:
            return [change.path for change in PendingChanges(self.store).status(held).changes]

    def live_release(self) -> str:
        return os.readlink(self.store.root / "generated")

    def previous_release(self) -> str | None:
        """The target of `generated.prev`, which `init-data` links back at boot."""
        link = self.store.root / "generated.prev"
        return os.readlink(link) if link.is_symlink() else None

    def get(self, host: str = HOST) -> httpx2.Response:
        return httpx2.get(
            f"https://{self.address}:{HTTPS_PORT}/",
            headers={"Host": host},
            verify=False,  # the default certificate is self-signed
            timeout=5.0,
        )

    def apply(self) -> ApplyResult:
        result = self.pipeline.apply("admin")
        assert result.stage != "validate", result
        return result


@pytest.fixture
def live(test_name_prefix: str) -> Iterator[Live]:
    """HAProxy on a placeholder release, then the first apply of one public route."""
    # Unix socket paths are limited to about 100 bytes, so the temp path stays short.
    data = Path(tempfile.mkdtemp(prefix="hp-"))
    run_dir = data / "run"
    run_dir.mkdir()
    shutil.copytree(SEED / "secrets", data / "secrets")
    (data / "config" / "routes").mkdir(parents=True)
    (data / "config" / "includes").mkdir()
    (data / "config" / "config.yaml").write_text(CONFIG)
    (data / "config" / "includes" / "upstream.cfg").write_bytes(UPSTREAM)
    placeholder = data / "releases" / "placeholder" / "conf.d"
    placeholder.mkdir(parents=True)
    (placeholder / "00-placeholder.cfg").write_text(PLACEHOLDER)
    (data / "generated").symlink_to("releases/placeholder")
    container = f"{test_name_prefix}-pipeline-{uuid.uuid4().hex[:12]}"
    try:
        docker(
            "run",
            "--detach",
            f"--name={container}",
            f"--user={os.getuid()}:{os.getgid()}",
            f"--volume={data}:/data",
            f"--volume={data}:{data}",
            f"--volume={run_dir}:/run/haproxy",
            haproxy_image(),
            "haproxy",
            "-W",
            "-S",
            "/run/haproxy/master.sock,mode,600,level,admin",
            "-f",
            "/data/generated/conf.d/",
        )
        address = docker(
            "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", container
        )
        client = HAProxyClient(str(run_dir / "master.sock"), str(run_dir / "admin.sock"))
        pipeline = Pipeline(
            Store(data),
            client=client,
            haproxy_command=("docker", "exec", container, "haproxy"),
            probe_host=address,
        )
        instance = Live(Store(data), address, pipeline)
        wait_for_haproxy(container, lambda: client.show_info().pid > 0)
        instance.stage("routes/app.yaml", route("app"))
        first = instance.apply()
        assert first.success, first
        yield instance
    finally:
        subprocess.run(["docker", "rm", "--force", container], capture_output=True, timeout=60)
        shutil.rmtree(data, ignore_errors=True)


def test_first_apply_reloads_serves_the_route_and_records_it(live: Live) -> None:
    [entry] = live.store.audit.read()
    releases = sorted(p.name for p in (live.store.root / "releases").iterdir())

    assert live.get().text == "ok"
    assert live.store.route_path("app").is_file()
    assert live.pending_paths() == []
    [snapshot] = Snapshots(live.store).list()
    assert (entry.details["path"], entry.details["snapshot"]) == ("reload", snapshot.id)
    # The History page names the route by its host, never by its internal name.
    assert snapshot.changes == (ChangeSummary(kind="route", action="added", label=HOST),)
    assert releases == sorted(["placeholder", Path(live.live_release()).name])
    assert (live.store.root / "state" / "haproxy-server-state").read_text().startswith("1\n")


def test_lan_public_toggle_applies_without_a_reload(live: Live) -> None:
    worker = live.client.show_info().pid
    live.stage("routes/app.yaml", route("app", exposure="lan"))

    result = live.apply()

    assert (result.success, result.path) == (True, "map")
    assert live.client.show_info().pid == worker
    # The test client is outside the LAN.
    assert live.get().status_code == 403
    assert "Strict-Transport-Security" not in live.get().headers
    # The release files hold the change, so a reload keeps it.
    assert live.client.reload().success
    assert live.get().status_code == 403


def test_host_moved_between_routes_applies_without_a_reload(live: Live) -> None:
    live.stage("routes/app.yaml", route("app", extra_hosts=", moved.example.com"))
    live.stage("routes/other.yaml", route("other", upstream="127.0.0.1:9001"))
    assert live.apply().success
    worker = live.client.show_info().pid
    live.stage("routes/app.yaml", route("app"))
    live.stage(
        "routes/other.yaml",
        route("other", upstream="127.0.0.1:9001", extra_hosts=", moved.example.com"),
    )

    result = live.apply()

    assert (result.success, result.path) == (True, "map")
    assert live.client.show_info().pid == worker
    assert live.get("moved.example.com").text == "other"


def test_apply_with_nothing_pending_reloads_the_live_release(live: Live) -> None:
    worker, release = live.client.show_info().pid, live.live_release()

    result = live.apply()

    assert (result.success, result.path) == (True, "reload")
    assert live.client.show_info().pid != worker
    assert live.live_release() == release


def test_apply_with_nothing_pending_snapshots_only_a_hand_edit(live: Live) -> None:
    """The Cloudflare reconcile runs only while `/data/config` equals the newest snapshot."""
    snapshots = Snapshots(live.store)
    assert live.apply().snapshot_id is None
    live.store.route_path("app").write_bytes(route("app", upstream="127.0.0.1:9001"))

    result = live.apply()

    assert live.get().text == "other"
    newest = snapshots.list()[0]
    assert (result.snapshot_id, newest.message) == (newest.id, "apply saved config")
    assert snapshots.is_applied()


def test_a_promote_that_stops_part_way_takes_no_snapshot(
    live: Live, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The edits are live but not all saved yet. The next operation under the lock finishes them."""
    live.stage("routes/other.yaml", route("other", upstream="127.0.0.1:9001"))
    before = Snapshots(live.store).list()

    def full_disk(path: Path, data: bytes, *, mode: int | None = None) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(pending_module, "atomic_write", full_disk)

    result = live.apply()

    assert (result.success, result.snapshot_id) == (True, None)
    assert "could not all be saved" in " ".join(result.warnings)
    assert Snapshots(live.store).list() == before


@dataclass(frozen=True)
class HandEditingClient(HAProxyClient):
    """Writes the staged `routes/other.yaml` by hand when the pipeline reads health, between switch
    and promote."""

    config_dir: str = ""

    def show_stat(self) -> list[StatRow]:
        (Path(self.config_dir) / "routes" / "other.yaml").write_bytes(b"# edited by hand\n")
        return super().show_stat()


@dataclass(frozen=True)
class LinkRecordingClient(HAProxyClient):
    """Records the release links when the pipeline reads health, between switch and promote."""

    data: str = ""
    seen: list[tuple[str, str | None]] = field(default_factory=list)

    def show_stat(self) -> list[StatRow]:
        root = Path(self.data)
        previous = root / "generated.prev"
        self.seen.append(
            (
                os.readlink(root / "generated"),
                os.readlink(previous) if previous.is_symlink() else None,
            )
        )
        return super().show_stat()


def test_previous_release_is_linked_from_the_switch_until_the_promote(live: Live) -> None:
    old = live.live_release()
    client = LinkRecordingClient(
        live.client.master_socket, live.client.admin_socket, data=str(live.store.root)
    )
    live.pipeline = replace(live.pipeline, client=client)
    live.stage("routes/other.yaml", route("other"))

    assert live.apply().success

    assert set(client.seen) == {(live.live_release(), old)}
    assert live.live_release() != old
    assert live.previous_release() is None


class Crash(BaseException):
    """Stands for the API process dying, which no `except Exception` in the pipeline can catch."""


@dataclass(frozen=True)
class CrashingClient(HAProxyClient):
    """Dies between the switch and the promote, after HAProxy runs the new release."""

    def show_stat(self) -> list[StatRow]:
        raise Crash


def test_api_crash_before_promote_is_recovered_at_the_next_start(live: Live) -> None:
    old = live.live_release()
    healthy = live.pipeline
    live.pipeline = replace(
        healthy, client=CrashingClient(live.client.master_socket, live.client.admin_socket)
    )
    live.stage("routes/other.yaml", route("other"))
    with pytest.raises(Crash):
        live.pipeline.apply("admin")
    assert live.previous_release() == old
    live.pipeline = healthy

    live.pipeline.recover()

    assert (live.live_release(), live.previous_release()) == (old, None)
    # HAProxy runs the old release again, so a discard would leave nothing live that is not saved.
    assert live.get("other.example.com").text != "ok"
    assert live.pending_paths() == ["routes/other.yaml"]
    assert live.apply().path == "reload"
    assert live.live_release() != old
    assert live.get("other.example.com").text == "ok"


def test_apply_that_changes_nothing_in_the_release_still_drops_generated_prev(live: Live) -> None:
    (live.store.root / "generated.prev").symlink_to("releases/placeholder")
    live.stage("routes/app.yaml", route("app") + b"# a comment\n")

    result = live.apply()

    assert (result.success, result.path) == (True, "none")
    assert live.previous_release() is None


def test_hand_edit_during_an_apply_rolls_back_at_promote(live: Live) -> None:
    release = live.live_release()
    client = HandEditingClient(
        live.client.master_socket, live.client.admin_socket, config_dir=str(live.store.config_dir)
    )
    live.pipeline = replace(live.pipeline, client=client)
    live.stage("routes/other.yaml", route("other"))

    result = live.apply()

    assert (result.success, result.stage) == (False, "promote")
    assert live.live_release() == release
    assert live.previous_release() is None
    assert live.get("other.example.com").status_code == 404
    assert live.get().text == "ok"


def test_broken_config_leaves_the_old_release_live_and_config_unchanged(live: Live) -> None:
    worker, release = live.client.show_info().pid, live.live_release()
    config = config_bytes(live.store)
    live.stage("includes/broken.cfg", b"bogus_keyword here\n")

    result = live.apply()

    assert (result.success, result.stage) == (False, "check")
    assert "bogus_keyword" in result.haproxy_log
    assert (live.live_release(), live.client.show_info().pid) == (release, worker)
    assert config_bytes(live.store) == config
    assert live.pending_paths() == ["includes/broken.cfg"]
    assert len(list((live.store.root / "releases").iterdir())) == 2
    assert live.get().text == "ok"


def test_failed_reload_rolls_back(live: Live) -> None:
    release, config = live.live_release(), config_bytes(live.store)
    # `haproxy -c` does not bind, so only the reload finds an address the host does not have.
    live.stage("includes/bind.cfg", b"listen unreachable\n    bind 192.0.2.1:8081\n")

    result = live.apply()

    assert (result.success, result.stage) == (False, "apply")
    assert "cannot bind socket" in result.haproxy_log
    assert live.live_release() == release
    assert config_bytes(live.store) == config
    assert live.pending_paths() == ["includes/bind.cfg"]
    assert live.get().text == "ok"


def test_failed_verification_rolls_back_with_a_reload(live: Live) -> None:
    release = live.live_release()
    # Nothing answers on this address, so the probe fails after a successful reload.
    live.pipeline = replace(live.pipeline, probe_host="192.0.2.1")
    live.stage("routes/other.yaml", route("other"))

    result = live.apply()

    assert (result.success, result.stage) == (False, "verify")
    assert live.live_release() == release
    assert live.previous_release() is None
    assert live.get("other.example.com").status_code == 404
    assert live.get().text == "ok"


def test_dead_upstream_warns_without_rollback(live: Live) -> None:
    """A down upstream and a host routed only on paths are warnings, not failures."""
    # A closed port fails the first check at once. Nothing answers at 192.0.2.1 (TEST-NET-1), so
    # its first check fails only at the connect timeout, and the second server's check starts last.
    live.stage("routes/dead.yaml", route("dead", upstream="127.0.0.1:9"))
    live.stage("routes/gone.yaml", route("gone", upstream="192.0.2.1:80") + b"path_prefix: /x\n")
    with live.store.lock() as held:
        PendingChanges(live.store).stage_delete("routes/app.yaml", held)

    result = live.apply()

    assert (result.success, result.path) == (True, "reload")
    assert result.warnings == (
        "gone.example.com is routed only on /x, so on the LAN its other paths get 404. Add a "
        "route for /, or move these paths to their own host.",
        "dead.example.com: upstream 127.0.0.1:9 is down",
        "gone.example.com/x: upstream 192.0.2.1:80 is down",
    )
    # `/status` reports route health live, so the audit keeps only the config warnings.
    assert live.store.audit.read()[0].details["warnings"] == [result.warnings[0]]
    assert live.store.route_path("dead").is_file()
    assert live.get("dead.example.com").status_code == 503


def test_reload_without_a_result_waits_for_the_new_worker(live: Live) -> None:
    worker = live.client.show_info().pid
    # The client gives up long before HAProxy answers, as on a slow reload.
    live.pipeline = replace(live.pipeline, client=replace(live.client, reload_timeout=0.001))
    live.stage("routes/other.yaml", route("other"))

    result = live.apply()

    assert (result.success, result.path) == (True, "reload"), result
    assert live.client.show_info().pid != worker
    assert live.get("other.example.com").text == "ok"
