"""Country blocking in a real HAProxy, after the refresh job built and applied the map.

The job downloads a small DB-IP file from a local stand-in. Clients need different source
addresses: the relays from `tests/integration/real_ip/` forward to the HTTPS frontend from fixed
loopback addresses, and the Cloudflare relay names any visitor in `CF-Connecting-IP`. The test
reaches the relays on the container's own address, so no host port is bound.
"""

import gzip
import http.client
import http.server
import os
import shutil
import subprocess
import tempfile
import threading
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from homeproxy.geoip.sources import URL_ENV
from homeproxy.haproxy.client import HAProxyClient
from homeproxy.haproxy.pipeline import Pipeline
from homeproxy.jobs.geoip import refresh
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

# The loopback sources are in CN, so the LAN client is in a blocked country too.
DBIP_ROWS = """\
127.0.0.0,127.255.255.255,CN
192.0.2.0,192.0.2.255,DE
198.51.100.0,198.51.100.255,CN
203.0.113.0,203.0.113.255,FR
2001:db8::,2001:db8:ffff:ffff:ffff:ffff:ffff:ffff,CN
"""

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
geoip:
  block_countries: [CN]
"""


def route(name: str, blocked: str, cloudflare: str = "{dns: true, proxied: true}") -> str:
    """Cloudflare's edge names the visitor only on a route the zone proxies, the default here."""
    return (
        f"version: 1\nname: {name}\ntype: http\nhosts: [{name}.example.com]\nexposure: public\n"
        f"upstreams:\n  - address: 127.0.0.1:9000\ngeoip: {{block_countries: [{blocked}]}}\n"
        f"cloudflare: {cloudflare}\n"
    )


@dataclass(frozen=True)
class Lab:
    address: str
    pipeline: Pipeline

    def status(self, source: str, route: str, visitor: str | None) -> int:
        connection = http.client.HTTPConnection(self.address, RELAY_PORTS[source], timeout=5)
        headers = {"Host": f"{route}.example.com"}
        if visitor is not None:
            headers["CF-Connecting-IP"] = visitor
        try:
            connection.request("GET", "/", headers=headers)
            return connection.getresponse().status
        finally:
            connection.close()


@pytest.fixture(scope="module")
def dbip_url() -> Iterator[str]:
    """Serves this month's DB-IP file."""
    body = gzip.compress(DBIP_ROWS.encode())
    path = f"/free/dbip-country-lite-{datetime.now(UTC):%Y-%m}.csv.gz"

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path != path:
                self.send_error(404)
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


def seed(data: Path) -> None:
    shutil.copytree(SEED / "secrets", data / "secrets")
    (data / "config" / "routes").mkdir(parents=True)
    (data / "config" / "includes").mkdir()
    (data / "config" / "config.yaml").write_text(CONFIG)
    (data / "config" / "includes" / "relays.cfg").write_text(RELAYS)
    (data / "config" / "routes" / "open.yaml").write_text(route("open", "FR"))
    (data / "config" / "routes" / "plain.yaml").write_text(route("plain", ""))
    (data / "config" / "routes" / "direct.yaml").write_text(route("direct", "", "{}"))
    (data / "config" / "routes" / "free.yaml").write_text(
        route("free", "").replace("geoip: {block_countries: []}", "geoip: {mode: replace}")
    )
    placeholder = data / "releases" / "placeholder" / "conf.d"
    placeholder.mkdir(parents=True)
    (placeholder / "00-placeholder.cfg").write_text(PLACEHOLDER)
    (data / "generated").symlink_to("releases/placeholder")


@pytest.fixture(scope="module")
def lab(dbip_url: str) -> Iterator[Lab]:
    """HAProxy after a first apply without GeoIP data, then a refresh that brings the map."""
    # Unix socket paths are limited to about 100 bytes, so the temp path stays short.
    data = Path(tempfile.mkdtemp(prefix="hp-"))
    container = f"homeproxy-test-geoip-{uuid.uuid4().hex[:12]}"
    run_dir = data / "run"
    run_dir.mkdir()
    seed(data)
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
            *("haproxy", "-W", "-S", "/run/haproxy/master.sock,mode,600,level,admin"),
            *("-f", "/data/generated/conf.d/"),
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
        assert pipeline.apply("admin").success
        lab = Lab(address, pipeline)
        assert lab.status(OTHER, "direct", None) == 200
        with pytest.MonkeyPatch.context() as patch:
            patch.setenv(URL_ENV, dbip_url)
            refresh(pipeline)
        # Only the test edge may name the visitor, until the next reload.
        client.replace_acl("cloudflare.lst", [f"{CLOUDFLARE}/32"])
        yield lab
    finally:
        subprocess.run(["docker", "rm", "--force", container], capture_output=True, timeout=60)
        shutil.rmtree(data, ignore_errors=True)


@pytest.mark.parametrize(
    ("source", "route", "visitor", "status"),
    [
        pytest.param(OTHER, "direct", None, 403, id="global-block"),
        pytest.param(
            CLOUDFLARE, "plain", "198.51.100.7", 403, id="global-block-through-cloudflare"
        ),
        pytest.param(CLOUDFLARE, "plain", "2001:db8::7", 403, id="ipv6-through-cloudflare"),
        pytest.param(CLOUDFLARE, "plain", "203.0.113.7", 200, id="route-list-stays-on-its-route"),
        pytest.param(CLOUDFLARE, "open", "203.0.113.7", 403, id="route-block"),
        pytest.param(CLOUDFLARE, "open", "192.0.2.7", 200, id="country-not-blocked"),
        pytest.param(CLOUDFLARE, "free", "198.51.100.7", 200, id="empty-replacing-list-is-open"),
        pytest.param(CLOUDFLARE, "open", "100.64.0.7", 200, id="address-without-country"),
        pytest.param(LAN, "plain", None, 200, id="lan-is-never-blocked"),
    ],
)
def test_blocked_country_gets_403(
    lab: Lab, source: str, route: str, visitor: str | None, status: int
) -> None:
    assert lab.status(source, route, visitor) == status


def test_a_newly_blocked_country_gets_403_after_one_apply(lab: Lab) -> None:
    """HAProxy's map holds only blocked countries, so blocking one more changes the map."""
    config = lab.pipeline.store.config_path
    text = config.read_text()
    config.write_text(text.replace("block_countries: [CN]", "block_countries: [CN, DE]"))
    try:
        assert lab.pipeline.apply("admin").success
        lab.pipeline.client.replace_acl("cloudflare.lst", [f"{CLOUDFLARE}/32"])
        assert lab.status(CLOUDFLARE, "plain", "192.0.2.7") == 403
    finally:
        config.write_text(text)
        assert lab.pipeline.apply("admin").success
        lab.pipeline.client.replace_acl("cloudflare.lst", [f"{CLOUDFLARE}/32"])
