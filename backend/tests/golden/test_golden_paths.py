"""Path routes in a real HAProxy: segment boundaries, the longest prefix and the stripped prefix.

The test upstream from `live_proxy` answers with the address it was reached on, and with
the path and query it got in `X-Path`. Each route on the shared host has its own upstream address.
"""

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from tests.golden.live_proxy import (
    NOT_THE_CLIENT,
    OTHER_ADDRESS_UPSTREAM,
    OTHER_PORT_UPSTREAM,
    UPSTREAM,
    Proxy,
    plain_route,
    running_proxy,
)

HOST = "paths.example.com"
ROUTES = (
    plain_route("app"),
    plain_route("site", hosts=[HOST]),
    plain_route(
        "api",
        hosts=[HOST],
        upstreams=[{"address": OTHER_PORT_UPSTREAM}],
        path_prefix="/api",
        strip_prefix=True,
    ),
    plain_route(
        "api-v2",
        hosts=[HOST],
        upstreams=[{"address": OTHER_ADDRESS_UPSTREAM}],
        path_prefix="/api/v2",
    ),
    plain_route("only", hosts=["only.example.com"], path_prefix="/api"),
)


@pytest.fixture(scope="module")
def proxy(make_data_dir: Callable[[], Path], haproxy_image: str) -> Iterator[Proxy]:
    with running_proxy(make_data_dir(), NOT_THE_CLIENT, haproxy_image, routes=ROUTES) as running:
        yield running


@pytest.mark.parametrize(
    ("path", "upstream", "upstream_path"),
    [
        pytest.param("/", UPSTREAM, "/", id="root"),
        pytest.param("/apiv2", UPSTREAM, "/apiv2", id="segment-boundary"),
        pytest.param("/api", OTHER_PORT_UPSTREAM, "/", id="strip-whole-path"),
        pytest.param("/api/x?q=1", OTHER_PORT_UPSTREAM, "/x?q=1", id="strip-keeps-query"),
        pytest.param("/api/v2x", OTHER_PORT_UPSTREAM, "/v2x", id="shorter-prefix"),
        pytest.param("/api/v2/x", OTHER_ADDRESS_UPSTREAM, "/api/v2/x", id="longest-prefix"),
        pytest.param("/%61pi/v2/x", OTHER_ADDRESS_UPSTREAM, "/%61pi/v2/x", id="decoded"),
        pytest.param("//api//v2", OTHER_ADDRESS_UPSTREAM, "//api//v2", id="slashes-merged"),
        pytest.param("/api;x/v2", OTHER_ADDRESS_UPSTREAM, "/api;x/v2", id="parameter-dropped"),
    ],
)
def test_request_reaches_the_route_of_its_longest_prefix(
    proxy: Proxy, path: str, upstream: str, upstream_path: str
) -> None:
    response = proxy.get(HOST, path=path)
    assert response.status == 200
    assert response.body.split("|")[0] == upstream
    assert response.headers["x-path"] == upstream_path


@pytest.mark.parametrize(
    "path",
    [
        "/x/../api",
        "/x/%2e%2e/api",
        "/api/.",
        "/x/..;/api/",
        "/%61pi/x",
        "/api/x%00",
        "/api/%zz",
        "/x%00/../api",
    ],
    ids=[
        "dot-segment",
        "encoded-dot-segment",
        "trailing-dot",
        "dot-segment-with-parameter",
        "prefix-spelled-otherwise",
        "encoded-nul",
        "bad-encoding",
        "bad-encoding-hides-dot-segment",
    ],
)
def test_path_that_could_reach_another_route_is_rejected(proxy: Proxy, path: str) -> None:
    """A path that does not decode would otherwise go to the `/` route. `/%61pi/x` matches `/api`
    decoded, but the prefix to cut is not in the raw path."""
    assert proxy.get(HOST, path=path).status == 400


def test_path_host_without_a_root_route_answers_404_outside_its_prefix(proxy: Proxy) -> None:
    assert proxy.get("only.example.com", path="/api/x").status == 200
    assert proxy.get("only.example.com", path="/").status == 404
