"""The real client IP behind Cloudflare, in a real HAProxy that the apply pipeline configured.

Clients need different source addresses. Relays in the same HAProxy forward to the HTTPS frontend
from fixed loopback addresses: one is Cloudflare's edge in the test copy of `cloudflare.lst`, one
is the LAN, one is neither. The refresh job accepts only public ranges, so the test copy is written
into the live release, as a refresh would write it. The test reaches the relays on the
container's own address, so no host port is bound. This needs Docker on the same kernel, because
the runtime sockets are bind-mounted.

The fixtures live in this file because `mypy` treats a second `conftest.py` as a duplicate.
"""

import http.client
import http.server
import os
import shutil
import subprocess
import tempfile
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

from homeproxy.generator.cloudflare_ranges import CLOUDFLARE_LIST
from homeproxy.haproxy.client import HAProxyClient
from homeproxy.haproxy.pipeline import Pipeline
from homeproxy.jobs.cloudflare_ranges import refresh
from homeproxy.store import Store
from tests.integration.haproxy_live import docker, haproxy_image, wait_for_haproxy
from tests.integration.relay_seed import (
    CLOUDFLARE,
    HTTP_PORT,
    HTTPS_PORT,
    LAN,
    OTHER,
    PASSWORD_HASH,
    PLACEHOLDER,
    RELAY_PORTS,
    RELAYS,
    SEED,
)

STARTUP_TIMEOUT = 30.0

# Public ranges that hold none of the test sources.
REFRESHED = {"/ips-v4": "1.1.1.0/24\n", "/ips-v6": "2606:4700::/32\n"}


CONFIG = f"""\
version: 1
dashboard:
  listen: 192.168.1.10:9443
  password_hash: "{PASSWORD_HASH}"
network:
  lan_cidrs: [{LAN}/32]
  haproxy_lan_ip: 192.168.1.10
  http_port: {HTTP_PORT}
  https_port: {HTTPS_PORT}
cloudflare:
  enabled: true
domains:
  - zone: example.com
    dns_provider: cloudflare
    credentials: cf-main
dns_server:
  listen: 192.168.1.10:5335
"""


def route(name: str, exposure: str, cloudflare: str = "{}") -> str:
    return (
        f"version: 1\nname: {name}\ntype: http\nhosts: [{name}.example.com]\n"
        f"exposure: {exposure}\nupstreams:\n  - address: 127.0.0.1:9000\n"
        f"cloudflare: {cloudflare}\n"
    )


ROUTES = {
    "open": route("open", "public"),
    "proxied": route("proxied", "public", "{dns: true, proxied: true}"),
    "private": route("private", "lan"),
}


@dataclass(frozen=True)
class Reply:
    status: int
    forwarded_for: str
    cf_headers: int
    relay_port: str


@dataclass(frozen=True)
class Live:
    """A running HAProxy, configured by one apply of `ROUTES`."""

    store: Store
    address: str
    pipeline: Pipeline

    @property
    def client(self) -> HAProxyClient:
        return self.pipeline.client

    def live_list(self) -> str:
        return ((self.store.root / "generated").resolve() / CLOUDFLARE_LIST).read_text()

    def get(self, source: str, route: str, *cf_values: str) -> list[Reply]:
        """Send one request per CF-Connecting-IP value, or one without it, on one connection."""
        connection = http.client.HTTPConnection(self.address, RELAY_PORTS[source], timeout=5)
        replies = []
        try:
            for value in cf_values or (None,):
                headers = {"Host": f"{route}.example.com"}
                if value is not None:
                    headers["CF-Connecting-IP"] = value
                connection.request("GET", "/", headers=headers)
                response = connection.getresponse()
                body = response.read().decode()
                forwarded_for, _, count = body.partition("|")
                cf_headers = int(count) if response.status == 200 else 0
                port = response.getheader("X-Relay-Port", "")
                replies.append(Reply(response.status, forwarded_for, cf_headers, port))
        finally:
            connection.close()
        return replies


def start_live(data: Path, container: str) -> Live:
    run_dir = data / "run"
    run_dir.mkdir()
    shutil.copytree(SEED / "secrets", data / "secrets")
    (data / "config" / "routes").mkdir(parents=True)
    (data / "config" / "includes").mkdir()
    (data / "config" / "config.yaml").write_text(CONFIG)
    (data / "config" / "includes" / "relays.cfg").write_text(RELAYS)
    for name, text in ROUTES.items():
        (data / "config" / "routes" / f"{name}.yaml").write_text(text)
    placeholder = data / "releases" / "placeholder" / "conf.d"
    placeholder.mkdir(parents=True)
    (placeholder / "00-placeholder.cfg").write_text(PLACEHOLDER)
    (data / "generated").symlink_to("releases/placeholder")
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
    wait_for_haproxy(container, lambda: client.show_info().pid > 0)
    result = pipeline.apply("admin")
    assert result.success, result
    return Live(Store(data), address, pipeline)


@contextmanager
def running() -> Iterator[Live]:
    """Run HAProxy after one apply, with the test edge as the only Cloudflare range."""
    # Unix socket paths are limited to about 100 bytes, so the temp path stays short.
    data = Path(tempfile.mkdtemp(prefix="hp-"))
    container = f"homeproxy-test-real-ip-{uuid.uuid4().hex[:12]}"
    try:
        live = start_live(data, container)
        (data / "generated").resolve().joinpath(CLOUDFLARE_LIST).write_text(f"{CLOUDFLARE}/32\n")
        assert live.client.reload().success
        yield live
    finally:
        subprocess.run(["docker", "rm", "--force", container], capture_output=True, timeout=60)
        shutil.rmtree(data, ignore_errors=True)


@pytest.fixture(scope="module")
def trusted() -> Iterator[Live]:
    """HAProxy that trusts the test edge, shared by the tests that change nothing."""
    with running() as live:
        yield live


@pytest.fixture
def edge() -> Iterator[Live]:
    """HAProxy that trusts the test edge, for one test that changes it."""
    with running() as live:
        yield live


@pytest.fixture
def serve() -> Iterator[Callable[[dict[str, str]], list[str]]]:
    """Return a function that serves `{path: text}` over HTTP and returns the two list URLs."""
    servers: list[http.server.ThreadingHTTPServer] = []

    def start(pages: dict[str, str]) -> list[str]:
        class Pages(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path not in pages:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.write(pages[self.path].encode())

            def log_message(self, format: str, *args: object) -> None:
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Pages)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return [f"http://127.0.0.1:{server.server_port}{path}" for path in REFRESHED]

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize(
    ("source", "route", "cf_value", "status", "client"),
    [
        pytest.param(CLOUDFLARE, "proxied", "198.51.100.7", 200, "198.51.100.7", id="edge-names"),
        pytest.param(CLOUDFLARE, "proxied", "2001:db8::7", 200, "2001:db8::7", id="ipv6-client"),
        pytest.param(OTHER, "open", "198.51.100.7", 200, OTHER, id="spoofed-header-ignored"),
        # Any Cloudflare account's Worker can reach a route your zone does not proxy.
        pytest.param(
            CLOUDFLARE, "open", "198.51.100.7", 200, CLOUDFLARE, id="edge-on-a-direct-route"
        ),
        pytest.param(CLOUDFLARE, "proxied", "unknown", 200, CLOUDFLARE, id="not-an-address"),
        pytest.param(CLOUDFLARE, "proxied", None, 200, CLOUDFLARE, id="proxied-from-cloudflare"),
        pytest.param(LAN, "proxied", None, 200, LAN, id="proxied-from-lan"),
        pytest.param(OTHER, "proxied", LAN, 403, None, id="proxied-direct-claiming-lan"),
        pytest.param(CLOUDFLARE, "private", LAN, 403, None, id="lan-only-via-cloudflare"),
    ],
)
def test_client_address_and_access(
    trusted: Live, source: str, route: str, cf_value: str | None, status: int, client: str | None
) -> None:
    [reply] = trusted.get(source, route, *([cf_value] if cf_value else []))

    assert reply.status == status
    if client is not None:
        # `option forwardfor` sends the address that every rule after the first ones sees.
        assert reply.forwarded_for == client
        # The header reaches the upstream only from Cloudflare, on a route it proxies.
        trusted_edge = source == CLOUDFLARE and route == "proxied"
        assert reply.cf_headers == (1 if trusted_edge and cf_value else 0)


def test_requests_sharing_a_connection_each_get_their_own_client(trusted: Live) -> None:
    """Cloudflare sends many visitors over one connection to the origin."""
    first, second = trusted.get(CLOUDFLARE, "proxied", "198.51.100.7", "198.51.100.8")

    assert first.relay_port == second.relay_port
    assert (first.forwarded_for, second.forwarded_for) == ("198.51.100.7", "198.51.100.8")


def test_refresh_swaps_the_ranges_without_a_reload_and_keeps_them(
    edge: Live, serve: Callable[[dict[str, str]], list[str]]
) -> None:
    worker = edge.client.show_info().pid
    [before] = edge.get(CLOUDFLARE, "proxied", "198.51.100.7")
    assert before.forwarded_for == "198.51.100.7"

    refresh(edge.store, edge.client, serve(REFRESHED))

    # The test edge is not in the new list, so the proxied route refuses it as a direct client.
    [after] = edge.get(CLOUDFLARE, "proxied", "198.51.100.7")
    assert after.status == 403
    assert edge.client.show_info().pid == worker
    # The live release holds the new list, so a reload keeps it, and the next apply renders it.
    refreshed = "".join(REFRESHED.values())
    assert edge.live_list() == refreshed
    assert edge.pipeline.apply("admin").success
    assert edge.live_list() == refreshed
