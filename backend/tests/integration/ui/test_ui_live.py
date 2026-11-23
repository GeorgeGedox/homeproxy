"""The built image serves the dashboard UI on the dashboard port, beside an unchanged API.

The test runs its own container (`tests/integration/image_server.py`), as it finishes setup. The
source rule acts on each connection before any request, so the UI's paths need no test of their
own (`tests/unit/auth/test_auth_server.py`).
"""

import re
from collections.abc import Iterator

import httpx2
import pytest

from tests.integration.image_server import (
    PASSWORD,
    Server,
    finish_setup,
    session_cookie,
    start_server,
)


@pytest.fixture(scope="module")
def server(test_name_prefix: str) -> Iterator[Server]:
    """A container in setup mode for this module."""
    with start_server(f"{test_name_prefix}-ui") as running:
        yield running


def assert_ui(response: httpx2.Response, index: str) -> None:
    """The response is the built `index.html`."""
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.text == index


def test_the_image_serves_the_ui_beside_the_api(server: Server) -> None:
    """The root and deep links get `index.html` with no session, its assets load with their
    type, and an unknown API path stays a JSON 404 for a signed-in client."""
    index = server.call("GET", "/").text
    for path in ("/", "/setup", "/routes/edit/app"):
        assert_ui(server.call("GET", path), index)
    assets = re.findall(r'(?:src|href)="(/_nuxt/[^"]+\.(js|css))"', index)
    assert {kind for _, kind in assets} == {"js", "css"}
    for path, kind in assets:
        response = server.call("GET", path)
        assert response.status_code == 200
        assert ("javascript" if kind == "js" else "text/css") in response.headers["content-type"]

    finish_setup(server)
    assert_ui(server.call("GET", "/routes"), index)
    login = server.call("POST", "/api/v1/auth/login", json={"password": PASSWORD})
    unknown = server.call("GET", "/api/v1/nope", session_cookie(login))
    assert unknown.status_code == 404
    assert unknown.headers["content-type"] == "application/json"
