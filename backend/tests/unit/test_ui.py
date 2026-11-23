"""Tests of the built dashboard served by the real app, with no session."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from homeproxy.main import create_app
from homeproxy.ui import mount_ui

ORIGIN = "https://testserver"
INDEX = "<!doctype html><title>HomeProxy</title>"


@pytest.fixture
def ui(tmp_path: Path) -> TestClient:
    """The real app serving a small built UI from a temporary directory."""
    (tmp_path / "_nuxt").mkdir()
    (tmp_path / "_nuxt" / "entry.js").write_text("console.log(1)")
    (tmp_path / "_nuxt" / "entry.css").write_text("body{}")
    (tmp_path / "index.html").write_text(INDEX)
    app = create_app()
    mount_ui(app, tmp_path)
    return TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN})


@pytest.mark.parametrize("path", ["/", "/setup", "/routes/edit/app", "/_nuxt/"])
def test_every_ui_path_gets_index_html_without_a_session(ui: TestClient, path: str) -> None:
    """The root and any deep link get `index.html`, so the client router takes over."""
    response = ui.get(path)
    assert response.status_code == 200
    assert response.text == INDEX
    assert response.headers["content-type"].startswith("text/html")


@pytest.mark.parametrize(
    ("path", "content_type"), [("/_nuxt/entry.js", "javascript"), ("/_nuxt/entry.css", "text/css")]
)
def test_assets_are_served_with_their_type(ui: TestClient, path: str, content_type: str) -> None:
    """A built asset is served as itself, not as `index.html`."""
    response = ui.get(path)
    assert response.status_code == 200
    assert content_type in response.headers["content-type"]


@pytest.mark.parametrize(
    ("method", "path", "status"),
    [("GET", "/api/v1/nope", 401), ("DELETE", "/api/v1/healthz", 405)],
)
def test_api_paths_never_fall_back_to_the_ui(
    ui: TestClient, method: str, path: str, status: int
) -> None:
    """An API path keeps its JSON answer: the session gate, or 405 for a wrong method."""
    response = ui.request(method, path)
    assert response.status_code == status
    assert response.headers["content-type"] == "application/json"


def test_without_the_ui_directory_only_the_api_is_served(tmp_path: Path) -> None:
    """The dev stack and unit tests have no built UI, and the API still answers."""
    app = create_app()
    mount_ui(app, tmp_path / "missing")
    client = TestClient(app)
    assert client.get("/").status_code == 404
    assert client.get("/api/v1/healthz").status_code == 200
