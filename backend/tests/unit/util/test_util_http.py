"""Outbound requests name HomeProxy, because some sites refuse Python's default user agent."""

import importlib.metadata
import threading
import urllib.request
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from homeproxy.util import http


class _PickySite(BaseHTTPRequestHandler):
    """Answers like db-ip.com and Cloudflare's IP lists (403 for `Python-urllib/*`) and like the
    CrowdSec LAPI (401 for an agent with no `/version`)."""

    def do_GET(self) -> None:
        agent = self.headers.get("User-Agent", "")
        name, slash, version = agent.partition("/")
        if name == "Python-urllib":
            self.send_response(403)
        else:
            self.send_response(200 if slash and version else 401)
        self.end_headers()
        self.wfile.write(agent.encode())

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def site() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _PickySite)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/ips-v4"
    server.shutdown()
    server.server_close()


def test_a_picky_site_accepts_homeproxy_with_its_version(site: str) -> None:
    with http.urlopen(site, timeout=5) as response:
        assert response.status == 200
        assert response.read().decode() == f"HomeProxy/{importlib.metadata.version('homeproxy')}"


class _MovedSite(BaseHTTPRequestHandler):
    """Redirects `/moved` and echoes the `Authorization` header it gets anywhere else."""

    def do_GET(self) -> None:
        if self.path == "/moved":
            self.send_response(302)
            self.send_header("Location", "/elsewhere")
            self.end_headers()
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(self.headers.get("Authorization", "none").encode())

    def log_message(self, format: str, *args: object) -> None:
        pass


def test_a_redirect_does_not_carry_the_token() -> None:
    """A Cloudflare or LAPI answer that redirects must not get the bearer token sent on."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _MovedSite)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        for path, expected in (("/direct", "Bearer secret"), ("/moved", "none")):
            headers = {"Authorization": "Bearer secret"}
            request = urllib.request.Request(base + path, headers=headers)  # noqa: S310
            with http.urlopen(request, timeout=5) as response:
                assert response.read().decode() == expected
    finally:
        server.shutdown()
        server.server_close()
