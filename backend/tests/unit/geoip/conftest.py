"""A local stand-in for the GeoIP download servers."""

import gzip
import threading
from collections.abc import Iterator
from datetime import UTC, date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from homeproxy.geoip.sources import URL_ENV


class GeoipServer(ThreadingHTTPServer):
    """Serves `pages` by path, without the query, and 404 for any other path."""

    pages: dict[str, bytes]
    status: int
    requests: list[str]

    def dbip(self, rows: str, month: date | None = None) -> None:
        """Serve DB-IP's file of `month`, by default this month's, with these CSV rows."""
        month = month or datetime.now(UTC).date()
        self.pages[f"/free/dbip-country-lite-{month:%Y-%m}.csv.gz"] = gzip.compress(rows.encode())


class Handler(BaseHTTPRequestHandler):
    server: GeoipServer

    def do_GET(self) -> None:
        self.server.requests.append(self.path)
        body = self.server.pages.get(self.path.partition("?")[0])
        if body is None or self.server.status != 200:
            self.send_error(404 if body is None else self.server.status)
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def geoip_server(monkeypatch: pytest.MonkeyPatch) -> Iterator[GeoipServer]:
    """A server that both providers' URLs point at."""
    server = GeoipServer(("127.0.0.1", 0), Handler)
    server.pages, server.status, server.requests = {}, 200, []
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    host, port = server.server_address[:2]
    monkeypatch.setenv(URL_ENV, f"http://{host!s}:{port}")
    yield server
    server.shutdown()
    server.server_close()
