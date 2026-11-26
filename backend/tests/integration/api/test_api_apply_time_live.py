"""One apply of 100 routes, timed, on the built image (#20).

An apply validates, renders, runs `haproxy -c` and reloads. With 100 routes it must stay under
5 seconds. It is one apply through the API, as the admin makes it, not a load test.
`test_geoip_footprint.py` times the apply of the largest country map with the same limit.
"""

import time
from collections.abc import Iterator
from typing import Any

import pytest

from tests.integration.image_server import (
    PASSWORD,
    TIMEOUT,
    Server,
    finish_setup,
    session_cookie,
    start_server,
)

APPLY_LIMIT = 5.0
ROUTES = 100
DOMAIN = {
    "zone": "example.com",
    "dns_provider": "cloudflare",
    "credentials": "cf-main",
    "api_token": "live-test-cloudflare-token",  # Cloudflare is a closed port
}


@pytest.fixture(scope="module")
def admin(test_name_prefix: str) -> Iterator[tuple[Server, str]]:
    """A set-up container in self-signed mode and a session cookie for it."""
    settings = ("HOMEPROXY_DEV_SELF_SIGNED=1", "CLOUDFLARE_BASE_URL=http://127.0.0.1:9")
    with start_server(f"{test_name_prefix}-apply-time", *settings) as server:
        finish_setup(server)
        login = server.call("POST", "/api/v1/auth/login", json={"password": PASSWORD})
        assert login.status_code == 204
        yield server, session_cookie(login)


def route(n: int) -> dict[str, Any]:
    return {
        "version": 1,
        "name": f"app{n}",
        "type": "http",
        "hosts": [f"app{n}.example.com"],
        "exposure": "public" if n % 2 else "lan",
        "upstreams": [{"address": f"127.0.0.1:{9000 + n}"}],
    }


def test_an_apply_of_100_routes_takes_under_5_seconds(admin: tuple[Server, str]) -> None:
    server, cookie = admin

    def call(method: str, path: str, **kwargs: Any) -> Any:
        response = server.call(method, f"/api/v1{path}", cookie, timeout=TIMEOUT, **kwargs)
        assert response.status_code < 300, response.text
        return response.json() if response.content else None

    call("POST", "/domains", json=DOMAIN)
    for n in range(ROUTES):
        call("POST", "/routes", json=route(n))

    started = time.monotonic()
    applied = call("POST", "/apply")
    seconds = time.monotonic() - started

    assert (applied["success"], applied["stage"]) == (True, "done"), applied
    assert applied["path"] == "reload"
    assert len(call("GET", "/routes")) == ROUTES
    assert seconds < APPLY_LIMIT, f"the apply of {ROUTES} routes took {seconds:.1f} s"
