"""The Cloudflare token check against a local fake of the two API calls it makes."""

import json
import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from homeproxy.certs.errors import TokenError
from homeproxy.certs.token import check_token

TOKEN = "unit-test-token-for-zone-check"  # noqa: S105 - a fake token
ZONE = {"id": "zone-1", "name": "example.com"}
NO_ACCESS = (403, {"success": False, "errors": [{"code": 9109, "message": "Unauthorized"}]})


def ok(result: Any) -> tuple[int, dict[str, Any]]:
    return 200, {"success": True, "errors": [], "result": result}


class FakeApi:
    """Answers by path prefix, and records the token each request carried."""

    def __init__(self) -> None:
        self.answers: dict[str, tuple[int, dict[str, Any]]] = {
            "/zones?": ok([ZONE]),
            "/zones/zone-1/dns_records": ok([]),
        }
        self.tokens: list[str] = []


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeApi]:
    fake = FakeApi()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            fake.tokens.append(self.headers.get("Authorization", ""))
            status, body = next(a for p, a in fake.answers.items() if self.path.startswith(p))
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("CLOUDFLARE_BASE_URL", f"http://127.0.0.1:{server.server_port}")
    yield fake
    server.shutdown()
    server.server_close()


def test_token_that_reads_the_zone_and_its_records_passes(api: FakeApi) -> None:
    check_token("example.com", TOKEN)

    assert api.tokens == [f"Bearer {TOKEN}"] * 2


@pytest.mark.parametrize(
    ("path", "answer", "message"),
    [
        pytest.param("/zones?", ok([]), "cannot read the zone example.com", id="no-zone-read"),
        pytest.param("/zones?", NO_ACCESS, "cannot read the zone example.com", id="zone-403"),
        pytest.param(
            "/zones?", ok([{"id": "z", "name": "other.example.com"}]), "cannot read the zone",
            id="other-zone-only",
        ),
        pytest.param(
            "/zones/zone-1/dns_records", NO_ACCESS, "cannot read DNS records in example.com",
            id="no-dns-read",
        ),
        pytest.param(
            "/zones?", (401, {"success": False, "errors": [{"code": 1000}]}), "invalid or expired",
            id="invalid-token",
        ),
    ],
)  # fmt: skip
def test_missing_permission_is_named_without_the_token(
    api: FakeApi, path: str, answer: tuple[int, dict[str, Any]], message: str
) -> None:
    api.answers[path] = answer

    with pytest.raises(TokenError, match=message) as failure:
        check_token("example.com", TOKEN)

    if "cannot read" in message:
        assert "Zone:Zone:Read and Zone:DNS:Edit" in str(failure.value)
    assert TOKEN not in str(failure.value)


def test_unreachable_api_is_a_token_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # A port that was free a moment ago refuses the connection at once.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    monkeypatch.setenv("CLOUDFLARE_BASE_URL", f"http://127.0.0.1:{port}")

    with pytest.raises(TokenError, match="Cloudflare could not be reached"):
        check_token("example.com", TOKEN)
