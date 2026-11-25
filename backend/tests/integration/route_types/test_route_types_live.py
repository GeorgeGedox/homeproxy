"""`passthrough`, `tcp`, `redirect` and plain HTTP routes in a real HAProxy that the apply pipeline
configured.

The passthrough upstream is a TLS frontend in the same HAProxy, from an include, with its own
certificate. Clients need different source addresses: TCP relays in the include forward to each
HomeProxy port from fixed loopback addresses, as in `tests/integration/real_ip/`. One of them is
in a blocked country of the test `country.map`. The access lines go to a real `LogCollector` in
this process. The test reaches HAProxy on the container's own address, so no host port is bound.
"""

import http.client
import os
import shutil
import socket
import ssl
import subprocess
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

from homeproxy.generator.certificate import DEFAULT_CERTIFICATE_NAME
from homeproxy.haproxy.client import HAProxyClient
from homeproxy.haproxy.pipeline import Pipeline
from homeproxy.logs import SYSLOG_SOCKET, AccessLog
from homeproxy.store import Store
from homeproxy.util.run import run
from tests.golden.live_proxy import CollectorThread
from tests.integration.haproxy_live import docker, haproxy_image, wait_for_haproxy
from tests.integration.relay_seed import (
    CLOUDFLARE,
    HTTP_PORT,
    HTTPS_PORT,
    LAN,
    OTHER,
    PASSWORD_HASH,
    PLACEHOLDER,
    SEED,
)

BLOCKED = "127.0.0.8"
VISITOR = "198.51.100.7"
GAME_PORT = 9100
SHELL_PORT = 9101
PAIR_PORT = 9102
SOURCES = (CLOUDFLARE, LAN, OTHER, BLOCKED)
TARGETS = (HTTPS_PORT, GAME_PORT, SHELL_PORT, HTTP_PORT)
UPSTREAM_CERTIFICATE = "upstream.pem"
UPSTREAM_TEXT = "passthrough upstream"
REQUEST = b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"


def relay_port(source: str, target: int) -> int:
    return 9200 + 10 * TARGETS.index(target) + SOURCES.index(source)


# The HTTP upstream answers with the client address that every rule saw. Relays are TCP, so the
# TLS of a passthrough host reaches its server unchanged.
INCLUDE = f"""\
listen test_upstream
    bind 127.0.0.1:9000
    http-request return status 200 content-type text/plain lf-string "%[req.fhdr(x-forwarded-for)]"

listen test_node_a
    bind 127.0.0.1:9001
    http-request return status 200 content-type text/plain string "node a"

listen test_node_b
    bind 127.0.0.1:9002
    http-request return status 200 content-type text/plain string "node b"

listen test_tls_upstream
    bind 127.0.0.1:9444 ssl crt /data/{UPSTREAM_CERTIFICATE}
    http-request return status 200 content-type text/plain string "{UPSTREAM_TEXT}"
""" + "".join(
    f"""
listen relay_{relay_port(source, target)} from tcp
    bind :{relay_port(source, target)}
    server target 127.0.0.1:{target} source {source}
"""
    for source in SOURCES
    for target in TARGETS
)

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


def route(name: str, kind: str, exposure: str, target: str, upstream: str) -> str:
    """`target` is the `hosts` line of a passthrough route, or the `listen_port` of a tcp one."""
    return (
        f"version: 1\nname: {name}\ntype: {kind}\n{target}\nexposure: {exposure}\n"
        f"upstreams:\n  - address: {upstream}\n"
    )


PASSTHROUGH_ROUTES = {
    "pass": route("pass", "passthrough", "public", "hosts: [pass.example.com]", "127.0.0.1:9444"),
    "vault": route("vault", "passthrough", "lan", "hosts: [vault.example.com]", "127.0.0.1:9444"),
}
WWW_ROUTE = """\
version: 1
name: www
type: redirect
hosts: [www.example.com]
exposure: public
redirect: {to: "https://example.com/", code: 301, keep_path: true}
"""
OLD_ROUTE = """\
version: 1
name: old
type: redirect
hosts: [old.example.com]
exposure: lan
redirect: {to: "https://app.example.com/start?from=old", code: 302}
"""
ROUTES = {
    **PASSTHROUGH_ROUTES,
    "www": WWW_ROUTE,
    "old": OLD_ROUTE,
    "app": route("app", "http", "public", "hosts: [app.example.com]", "127.0.0.1:9000"),
    # Cloudflare's edge names the visitor only on a route the zone proxies.
    "edge": route("edge", "http", "public", "hosts: [edge.example.com]", "127.0.0.1:9000")
    + "cloudflare: {dns: true, proxied: true}\n",
    "game": route("game", "tcp", "public", f"listen_port: {GAME_PORT}", "127.0.0.1:9000"),
    "shell": route("shell", "tcp", "lan", f"listen_port: {SHELL_PORT}", "127.0.0.1:9000"),
    "pair": route("pair", "tcp", "public", f"listen_port: {PAIR_PORT}", "127.0.0.1:9001")
    + "  - address: 127.0.0.1:9002\nsticky: true\n",
}


def der_certificate(pem: str) -> bytes:
    """Return the first certificate of a PEM file, as a TLS handshake shows it."""
    begin, end = "-----BEGIN CERTIFICATE-----", "-----END CERTIFICATE-----"
    block = pem[pem.index(begin) : pem.index(end) + len(end)]
    return ssl.PEM_cert_to_DER_cert(block)


@dataclass(frozen=True)
class Reply:
    certificate: bytes
    status: int
    body: str


@dataclass(frozen=True)
class Live:
    """A running HAProxy, configured by one apply of `ROUTES`."""

    data: Path
    container: str
    address: str
    pipeline: Pipeline
    collector: CollectorThread

    def certificate(self, name: str) -> bytes:
        return der_certificate((self.data / name).read_text())

    def tls(self, port: int, sni: str) -> ssl.SSLSocket:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        raw = socket.create_connection((self.address, port), timeout=5)
        return context.wrap_socket(raw, server_hostname=sni)

    def https(
        self,
        port: int,
        sni: str,
        *hosts: str,
        headers: dict[str, str] | None = None,
        path: str = "/",
    ) -> list[Reply]:
        """Send one request per host over one TLS connection with this SNI name."""
        connection = http.client.HTTPConnection(self.address, port, timeout=5)
        connection.sock = self.tls(port, sni)
        certificate = connection.sock.getpeercert(binary_form=True) or b""
        replies = []
        try:
            for host in hosts:
                connection.request("GET", path, headers={"Host": host, **(headers or {})})
                response = connection.getresponse()
                replies.append(Reply(certificate, response.status, response.read().decode()))
        finally:
            connection.close()
        return replies

    def request(self, port: int, host: str, path: str, *, tls: bool) -> tuple[int, str | None]:
        """Send one request and return its status and `Location` header."""
        connection = http.client.HTTPConnection(self.address, port, timeout=5)
        if tls:
            connection.sock = self.tls(port, host)
        try:
            connection.request("GET", path, headers={"Host": host})
            response = connection.getresponse()
            return response.status, response.getheader("Location")
        finally:
            connection.close()

    def passthrough_served(self, source: str, host: str) -> bool:
        """Return whether a TLS connection from `source` with SNI `host` reaches its server."""
        try:
            [reply] = self.https(relay_port(source, HTTPS_PORT), host, host)
        except (OSError, http.client.HTTPException):
            return False
        return reply.body == UPSTREAM_TEXT

    def tcp_reply(self, source: str, port: int, request: bytes) -> bytes:
        """Send raw TCP from `source` to a tcp route's port, and return all that comes back."""
        reply = b""
        try:
            with socket.create_connection((self.address, relay_port(source, port)), 5) as client:
                client.sendall(request)
                while chunk := client.recv(4096):
                    reply += chunk
        except OSError:
            return reply
        return reply

    def log_entries(self, frontend: str, client: str) -> list[AccessLog]:
        """Return the access lines of this frontend that name this client."""
        return [
            entry
            for entry in self.collector.entries()
            if (entry.frontend, entry.client_ip) == (frontend, client)
        ]

    def next_log_entry(self, frontend: str, client: str, before: int) -> AccessLog:
        """Wait for one more such line than `before`, and return the newest."""
        wait_for_haproxy(
            self.container, lambda: len(self.log_entries(frontend, client)) > before, "the log line"
        )
        return self.log_entries(frontend, client)[-1]

    def apply(self) -> None:
        result = self.pipeline.apply("admin")
        assert result.success, result
        # Only the test edge may name the visitor, until the next reload.
        self.pipeline.client.replace_acl("cloudflare.lst", [f"{CLOUDFLARE}/32"])


def seed(data: Path) -> None:
    shutil.copytree(SEED / "secrets", data / "secrets")
    (data / "config" / "routes").mkdir(parents=True)
    (data / "config" / "includes").mkdir()
    (data / "config" / "config.yaml").write_text(CONFIG)
    (data / "config" / "includes" / "relays.cfg").write_text(INCLUDE)
    for name, text in ROUTES.items():
        (data / "config" / "routes" / f"{name}.yaml").write_text(text)
    (data / "geoip").mkdir()
    (data / "geoip" / "country.map").write_text(f"{BLOCKED}/32 CN\n")
    certificate = data / UPSTREAM_CERTIFICATE
    run(
        [
            *("openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256"),
            *("-nodes", "-keyout", str(certificate), "-out", str(certificate)),
            *("-days", "1", "-subj", "/CN=passthrough upstream"),
        ]
    )
    placeholder = data / "releases" / "placeholder" / "conf.d"
    placeholder.mkdir(parents=True)
    (placeholder / "00-placeholder.cfg").write_text(PLACEHOLDER)
    (data / "generated").symlink_to("releases/placeholder")


@contextmanager
def running() -> Iterator[Live]:
    """Run HAProxy after one apply of `ROUTES`, with the test edge as the only Cloudflare range."""
    # Unix socket paths are limited to about 100 bytes, so the temp path stays short.
    data = Path(tempfile.mkdtemp(prefix="hp-"))
    container = f"homeproxy-test-route-types-{uuid.uuid4().hex[:12]}"
    run_dir, log_dir = data / "run", data / "log"
    run_dir.mkdir()
    log_dir.mkdir()
    seed(data)
    try:
        with CollectorThread(log_dir / Path(SYSLOG_SOCKET).name) as collector:
            docker(
                "run",
                "--detach",
                f"--name={container}",
                f"--user={os.getuid()}:{os.getgid()}",
                f"--volume={data}:/data",
                f"--volume={data}:{data}",
                f"--volume={run_dir}:/run/haproxy",
                f"--volume={log_dir}:{Path(SYSLOG_SOCKET).parent}",
                haproxy_image(),
                *("haproxy", "-W", "-S", "/run/haproxy/master.sock,mode,600,level,admin"),
                *("-f", "/data/generated/conf.d/"),
            )
            address = docker(
                "inspect",
                "-f",
                "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                container,
            )
            client = HAProxyClient(str(run_dir / "master.sock"), str(run_dir / "admin.sock"))
            pipeline = Pipeline(
                Store(data),
                client=client,
                haproxy_command=("docker", "exec", container, "haproxy"),
                probe_host=address,
            )
            wait_for_haproxy(container, lambda: client.show_info().pid > 0)
            live = Live(data, container, address, pipeline, collector)
            live.apply()
            yield live
    finally:
        subprocess.run(["docker", "rm", "--force", container], capture_output=True, timeout=60)
        shutil.rmtree(data, ignore_errors=True)


@pytest.fixture(scope="module")
def live() -> Iterator[Live]:
    """HAProxy shared by the tests that change nothing."""
    with running() as running_live:
        yield running_live


@pytest.fixture
def own_live() -> Iterator[Live]:
    """HAProxy for one test that changes its routes."""
    with running() as running_live:
        yield running_live


def test_passthrough_host_gets_the_backends_own_certificate(live: Live) -> None:
    [passthrough] = live.https(HTTPS_PORT, "pass.example.com", "pass.example.com")
    assert passthrough.certificate == live.certificate(UPSTREAM_CERTIFICATE)
    assert passthrough.body == UPSTREAM_TEXT

    # Every other name still gets HomeProxy's certificate and its HTTP routes.
    [terminated] = live.https(HTTPS_PORT, "app.example.com", "app.example.com")
    assert terminated.certificate == live.certificate(f"certs/{DEFAULT_CERTIFICATE_NAME}")
    assert terminated.status == 200


def test_reused_connection_to_a_passthrough_host_gets_421(live: Live) -> None:
    """A browser reuses a connection for any host that the certificate covers."""
    app, passthrough = live.https(
        HTTPS_PORT, "app.example.com", "app.example.com", "pass.example.com"
    )
    assert app.status == 200
    assert passthrough.status == 421


@pytest.mark.parametrize(
    ("source", "route", "served"),
    [
        pytest.param(OTHER, "game", True, id="tcp-forwards-its-port"),
        pytest.param(OTHER, "shell", False, id="tcp-lan-only-refuses-outside"),
        pytest.param(LAN, "shell", True, id="tcp-lan-only-serves-lan"),
        pytest.param(BLOCKED, "game", False, id="tcp-blocked-country"),
        pytest.param(OTHER, "pass", True, id="passthrough-public"),
        pytest.param(OTHER, "vault", False, id="passthrough-lan-only-refuses-outside"),
        pytest.param(LAN, "vault", True, id="passthrough-lan-only-serves-lan"),
        pytest.param(BLOCKED, "pass", False, id="passthrough-blocked-country"),
    ],
)
def test_access_rules_apply_to_the_real_client(
    live: Live, source: str, route: str, served: bool
) -> None:
    if route in PASSTHROUGH_ROUTES:
        assert live.passthrough_served(source, f"{route}.example.com") is served
        return
    # A refused client sends nothing, as an SSH client waits for the server. It is still logged.
    before = len(live.log_entries("fe_tcp", source))
    port = GAME_PORT if route == "game" else SHELL_PORT
    reply = live.tcp_reply(source, port, REQUEST if served else b"")
    assert reply.startswith(b"HTTP/1.1 200 ") is served
    assert live.next_log_entry("fe_tcp", source, before).route == route


def test_sticky_route_keeps_a_client_on_one_upstream(live: Live) -> None:
    nodes = set()
    for _ in range(6):
        with socket.create_connection((live.address, PAIR_PORT), 5) as client:
            client.sendall(REQUEST)
            reply = b""
            while chunk := client.recv(4096):
                reply += chunk
        nodes.add(reply.rsplit(b"\r\n\r\n", 1)[-1])
    assert len(nodes) == 1
    assert nodes <= {b"node a", b"node b"}


def test_logs_name_the_real_client_behind_the_sni_layer(live: Live) -> None:
    # The PROXY header carries the client from the SNI layer to fe_passthrough.
    before = len(live.log_entries("fe_passthrough", OTHER))
    assert live.passthrough_served(OTHER, "pass.example.com")
    assert live.next_log_entry("fe_passthrough", OTHER, before).route == "pass"

    # Cloudflare's edge names the visitor behind the SNI layer, as without it.
    before = len(live.log_entries("fe_https", VISITOR))
    port = relay_port(CLOUDFLARE, HTTPS_PORT)
    visitor = {"CF-Connecting-IP": VISITOR}
    edge = live.https(port, "edge.example.com", "edge.example.com", headers=visitor)
    assert [(reply.status, reply.body) for reply in edge] == [(200, VISITOR)]
    assert live.next_log_entry("fe_https", VISITOR, before).route == "edge"


def test_sni_layer_is_there_only_while_a_passthrough_route_is(own_live: Live) -> None:
    routes = own_live.data / "config" / "routes"
    for name in PASSTHROUGH_ROUTES:
        (routes / f"{name}.yaml").unlink()
    own_live.apply()
    assert "fe_sni" not in (own_live.data / "generated" / "conf.d" / "10-haproxy.cfg").read_text()
    [reply] = own_live.https(HTTPS_PORT, "pass.example.com", "pass.example.com")
    assert reply.certificate == own_live.certificate(f"certs/{DEFAULT_CERTIFICATE_NAME}")
    assert reply.status == 404

    for name, text in PASSTHROUGH_ROUTES.items():
        (routes / f"{name}.yaml").write_text(text)
    own_live.apply()
    assert own_live.passthrough_served(OTHER, "pass.example.com")


def test_www_redirects_to_the_apex_with_the_path_kept(live: Live) -> None:
    """Plain HTTP is in `tests/golden/test_golden_plain_http.py`."""
    reply = live.request(HTTPS_PORT, "www.example.com", "/a/b?c=1&d", tls=True)
    assert reply == (301, "https://example.com/a/b?c=1&d")


@pytest.mark.parametrize(
    ("lan", "tls", "expected"),
    [
        pytest.param(False, False, (403, None), id="outside-http"),
        pytest.param(False, True, (403, None), id="outside-https"),
        pytest.param(True, False, (302, "https://app.example.com/start?from=old"), id="lan-http"),
    ],
)
def test_lan_only_redirect_answers_only_the_lan(
    live: Live, lan: bool, tls: bool, expected: tuple[int, str | None]
) -> None:
    """The checks run before the redirect on plain HTTP too. The test client is outside the LAN."""
    port = HTTPS_PORT if tls else HTTP_PORT
    reply = live.request(relay_port(LAN, port) if lan else port, "old.example.com", "/x", tls=tls)
    assert reply == expected


def test_changed_redirect_target_needs_no_reload(own_live: Live) -> None:
    route = own_live.data / "config" / "routes" / "www.yaml"
    old = '{to: "https://example.com/", code: 301, keep_path: true}'
    route.write_text(WWW_ROUTE.replace(old, '{to: "https://example.net/new", code: 308}'))
    result = own_live.pipeline.apply("admin")
    assert (result.success, result.path) == (True, "map")
    reply = own_live.request(HTTP_PORT, "www.example.com", "/a", tls=False)
    assert reply == (308, "https://example.net/new")
