"""An apply through the built image reaches the DNS process's answers.

The container runs in self-signed mode, so adding a domain needs no Cloudflare. Setup points the
DNS listener at the container's address, which the test queries directly, so no port is published.
"""

import time
from collections.abc import Iterator
from typing import Any

import dns.exception
import dns.message
import dns.query
import pytest

from tests.integration.image_server import (
    PASSWORD,
    Server,
    finish_setup,
    session_cookie,
    start_server,
)

DNS_PORT = 5335
RESOLVE_WITHIN = 2.0
"""The product promise: a new route resolves within 2 s of apply."""
DOMAIN = {
    "zone": "example.com",
    "dns_provider": "cloudflare",
    "credentials": "cf-main",
    "api_token": "live-test-cloudflare-token",
}
ROUTE = {
    "version": 1,
    "name": "app",
    "type": "http",
    "hosts": ["app.example.com"],
    "exposure": "lan",
    "upstreams": [{"address": "127.0.0.1:9"}],
}


@pytest.fixture(scope="module")
def admin(test_name_prefix: str) -> Iterator[tuple[Server, str]]:
    """A set-up container in self-signed mode and a session cookie for it."""
    with start_server(f"{test_name_prefix}-dns", "HOMEPROXY_DEV_SELF_SIGNED=1") as server:
        finish_setup(server)
        login = server.call("POST", "/api/v1/auth/login", json={"password": PASSWORD})
        assert login.status_code == 204
        yield server, session_cookie(login)


def assert_resolves(server: Server, name: str, expected: str) -> None:
    """Query `name` until it answers `expected`, or fail after `RESOLVE_WITHIN` seconds."""
    deadline = time.monotonic() + RESOLVE_WITHIN
    answer: list[str] = []
    while time.monotonic() < deadline:
        query = dns.message.make_query(name, "A")
        try:
            response = dns.query.udp(query, server.ip, port=DNS_PORT, timeout=0.2)
        except (dns.exception.Timeout, OSError):
            continue
        answer = [rdata.to_text() for rrset in response.answer for rdata in rrset]
        if answer == [expected]:
            return
    pytest.fail(f"{name} answered {answer} within {RESOLVE_WITHIN}s, expected {expected}")


def test_an_applied_route_resolves_within_two_seconds(admin: tuple[Server, str]) -> None:
    """A new route resolves to the proxy's LAN IP. A changed DNS target alone needs no reload."""
    server, cookie = admin

    def call(method: str, path: str, **kwargs: Any) -> Any:
        response = server.call(method, f"/api/v1{path}", cookie, **kwargs)
        assert response.status_code < 300, response.text
        return response.json() if response.content else None

    call("POST", "/domains", json=DOMAIN)
    call("POST", "/routes", json=ROUTE)
    first = call("POST", "/apply")

    assert first["success"], first
    assert_resolves(server, "app.example.com", server.ip)

    call("PUT", "/routes/app", json=ROUTE | {"dns": {"target": "192.168.1.20"}})
    moved = call("POST", "/apply")

    assert (moved["success"], moved["path"]) == (True, "map"), moved
    assert_resolves(server, "app.example.com", "192.168.1.20")
