"""Access logs of generated config in a real HAProxy. HAProxy sends every line to a real
`LogCollector` in this process, which these tests read."""

import socket
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from homeproxy.logs import HTTP_LOG_FORMAT, TCP_LOG_FORMAT, AccessLog
from tests.golden.live_proxy import (
    LOG_FORMATS_PATH,
    NOT_THE_CLIENT,
    PASSTHROUGH_HOST,
    UPSTREAM,
    Proxy,
    plain_route,
    running_proxy,
    tls_request,
)

CLOUDFLARE_ZONE = {"zone": "example.com", "dns_provider": "cloudflare", "credentials": "cf-main"}
SETTLE = 1.0
"""How long the test of each connection logging once waits after the expected lines arrive, so
that an extra line would show. No later signal shows that HAProxy sent everything."""


def _marker_path() -> str:
    return f"/log-test/{uuid.uuid4().hex}"


def _wait_for_entries(
    proxy: Proxy, matches: Callable[[AccessLog], bool], count: int = 1, settle: float = 0.0
) -> list[AccessLog]:
    """Return the collected lines that match, `settle` seconds after there are `count` of them."""
    deadline = time.monotonic() + 15
    while len([entry for entry in proxy.collector.entries() if matches(entry)]) < count:
        if time.monotonic() > deadline:
            lines = "\n".join(entry.line for entry in proxy.collector.entries()[-50:])
            raise AssertionError(f"fewer than {count} matching log lines:\n{lines}")
        time.sleep(0.1)
    time.sleep(settle)
    return [entry for entry in proxy.collector.entries() if matches(entry)]


def _for_path(path: str) -> Callable[[AccessLog], bool]:
    """Match the lines of a request, except the line of the test upstream that served it."""
    return lambda entry: (
        entry.frontend != "test_upstream"
        and entry.request is not None
        and f" {path} " in entry.request
    )


def test_log_formats_start_with_haproxys_default_formats(outside_proxy: Proxy) -> None:
    """The CrowdSec HAProxy parser expects HAProxy's defaults, so the fields must not drift."""
    response = outside_proxy.get("app.example.com", path=LOG_FORMATS_PATH)
    assert response.body.split("|") == [HTTP_LOG_FORMAT, TCP_LOG_FORMAT]


def test_https_request_is_logged_with_route_and_sni(outside_proxy: Proxy) -> None:
    path = _marker_path()
    reply = tls_request(outside_proxy.https_port, "app.example.com", "app.example.com", path)
    assert reply.startswith("HTTP/1.1 200 ")
    [entry] = _wait_for_entries(outside_proxy, _for_path(path))
    assert entry.kind == "http"
    assert (entry.frontend, entry.tls, entry.backend) == ("fe_https", True, "route-app")
    assert (entry.status, entry.method, entry.request) == (200, "GET", f"GET {path} HTTP/1.1")
    assert (entry.route, entry.country, entry.sni) == ("app", None, "app.example.com")


def test_denied_and_unknown_requests_are_logged(outside_proxy: Proxy) -> None:
    path = _marker_path()
    assert outside_proxy.get("private.example.com", path=path).status == 403
    assert outside_proxy.get("unknown.example.com", path=path).status == 404
    found = _wait_for_entries(outside_proxy, _for_path(path), 2)
    entries = {entry.status: entry for entry in found}
    assert entries.keys() == {403, 404}
    # A request the LAN rule denies still counts toward its route.
    assert entries[403].route == "private"
    assert (entries[404].backend, entries[404].route) == ("be_unknown_host", None)


def test_only_a_host_that_no_route_serves_is_logged_as_unknown(
    data_dir: Path, haproxy_image: str
) -> None:
    """The CrowdSec scenario in `docs/crowdsec.md` bans a client that asks twice for an unknown
    host. A host that a route serves, or served before it was switched off, is never unknown, even
    on a path or a port that it does not answer on."""
    routes = (
        plain_route("app"),
        plain_route("api", hosts=["paths.example.com"], path_prefix="/api"),
        plain_route("off", enabled=False),
        plain_route("plain", hosts=["plain.example.net"]),
    )
    domains = {"domains": [CLOUDFLARE_ZONE, {"zone": "example.net"}]}
    path = _marker_path()
    with running_proxy(data_dir, NOT_THE_CLIENT, haproxy_image, routes, domains) as proxy:
        hosts = ["unknown.example.com", "paths.example.com", "off.example.com", "plain.example.net"]
        for host in hosts:
            assert tls_request(proxy.https_port, host, host, path).startswith("HTTP/1.1 404")
        found = _wait_for_entries(proxy, _for_path(path), len(hosts))
    assert [(entry.sni, entry.backend, entry.route) for entry in found] == [
        ("unknown.example.com", "be_unknown_host", None),
        ("paths.example.com", "be_not_found", None),
        ("off.example.com", "be_not_found", None),
        ("plain.example.net", "be_not_found", None),
    ]


def test_http_redirect_is_logged_without_tls(outside_proxy: Proxy) -> None:
    path = _marker_path()
    assert outside_proxy.get_plain("app.example.com", path=path).status == 301
    [entry] = _wait_for_entries(outside_proxy, _for_path(path))
    assert (entry.frontend, entry.tls, entry.status) == ("fe_https", False, 301)
    assert (entry.route, entry.sni) == (None, None)


def test_http_log_names_the_visitor_only_through_cloudflare(
    data_dir: Path, haproxy_image: str
) -> None:
    """CrowdSec bans the addresses in these lines. Cloudflare's edge must never be one, and no one
    else may put a victim's address there. Only a route the zone proxies takes the header."""
    routes = (plain_route("edge", cloudflare={"dns": True, "proxied": True}), plain_route("app"))
    changes = {"cloudflare": {"enabled": True}, "domains": [CLOUDFLARE_ZONE]}
    visitor = (("CF-Connecting-IP", "198.51.100.9"),)
    with running_proxy(data_dir, NOT_THE_CLIENT, haproxy_image, routes, changes) as proxy:
        path = _marker_path()
        assert proxy.get_plain("edge.example.com", path=path, extra=visitor).status == 301
        [direct] = _wait_for_entries(proxy, _for_path(path))
        assert direct.client_ip != "198.51.100.9"
        # The test client becomes Cloudflare's edge.
        proxy.runtime(f"add acl cloudflare.lst {direct.client_ip}")
        entries = {}
        for host in ("edge", "app"):
            path = _marker_path()
            assert proxy.get_plain(f"{host}.example.com", path=path, extra=visitor).status == 301
            [entries[host]] = _wait_for_entries(proxy, _for_path(path))
    assert (entries["edge"].frontend, entries["edge"].client_ip) == ("fe_https", "198.51.100.9")
    assert entries["app"].client_ip == direct.client_ip


def test_client_sni_reaches_the_log_encoded(outside_proxy: Proxy) -> None:
    """The SNI is the only appended field the client controls. Raw, a newline or a quote in it
    could end the line or the field, and a forged line could get someone else banned."""
    path = _marker_path()
    reply = tls_request(outside_proxy.https_port, 'a\nb"c d\\e]', "app.example.com", path)
    assert reply.startswith("HTTP/1.1 200 ")
    [entry] = _wait_for_entries(outside_proxy, _for_path(path))
    assert entry.sni == "a%0Ab%22c%20d%5Ce%5D"
    assert entry.route == "app"


def test_tcp_connection_is_logged_in_the_tcp_format(outside_proxy: Proxy) -> None:
    before = len(outside_proxy.entries("fe_tcp"))
    with socket.create_connection(("127.0.0.1", outside_proxy.tcp_port), timeout=10) as client:
        client.sendall(b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        while client.recv(4096):
            pass
    entries = _wait_for_entries(outside_proxy, lambda entry: entry.frontend == "fe_tcp", before + 1)
    assert len(entries) == before + 1
    entry = entries[-1]
    assert (entry.kind, entry.status, entry.request, entry.tls) == ("tcp", None, None, False)
    assert (entry.backend, entry.server) == ("route-game", UPSTREAM)
    assert (entry.route, entry.sni) == ("game", None)
    assert entry.bytes_sent > 0


def test_sni_layer_logs_nothing_and_each_connection_once_with_the_real_client(
    outside_proxy: Proxy,
) -> None:
    def passed_through(entry: AccessLog) -> bool:
        return entry.frontend == "fe_passthrough"

    before = len(outside_proxy.entries("fe_passthrough"))
    path = _marker_path()
    port = outside_proxy.https_port
    assert tls_request(port, "app.example.com", "app.example.com", path).startswith("HTTP/1.1 200")
    [https_entry] = _wait_for_entries(outside_proxy, _for_path(path), settle=SETTLE)
    assert (https_entry.frontend, https_entry.sni) == ("fe_https", "app.example.com")

    assert tls_request(port, PASSTHROUGH_HOST, PASSTHROUGH_HOST, "/").startswith("HTTP/1.1 200")
    passthrough_entries = _wait_for_entries(outside_proxy, passed_through, before + 1, SETTLE)
    assert len(passthrough_entries) == before + 1
    passthrough = passthrough_entries[-1]
    assert (passthrough.kind, passthrough.backend) == ("tcp", "route-pass")
    assert (passthrough.route, passthrough.sni) == ("pass", PASSTHROUGH_HOST)
    assert outside_proxy.entries("fe_sni") == []

    # Port 80 has no SNI layer, so its line names the client directly. Behind the layer, the
    # PROXY header carries the same client, or both frontends would log `unix`.
    path = _marker_path()
    assert outside_proxy.get_plain("app.example.com", path=path).status == 301
    [direct] = _wait_for_entries(outside_proxy, _for_path(path))
    assert https_entry.client_ip == passthrough.client_ip == direct.client_ip


def test_haproxy_keeps_serving_while_the_collector_is_down(
    data_dir: Path, haproxy_image: str
) -> None:
    with running_proxy(data_dir, NOT_THE_CLIENT, haproxy_image, (plain_route("app"),)) as proxy:
        proxy.collector.stop()
        for _ in range(20):
            assert proxy.get("app.example.com").status == 200
        # A restarted API binds the socket again, and HAProxy sends to it without a reload.
        proxy.collector.start()
        path = _marker_path()
        assert proxy.get("app.example.com", path=path).status == 200
        [entry] = _wait_for_entries(proxy, _for_path(path))
        assert entry.route == "app"
