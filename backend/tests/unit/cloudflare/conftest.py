"""A local fake of the Cloudflare DNS records API, with zones, records and a call log."""

import json
import re
import shutil
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from homeproxy.store import Store
from homeproxy.store.snapshots import Snapshots

DATA = Path(__file__).parents[4] / "examples" / "data"
TOKENS = {"example.com": "fake-cloudflare-token-one", "example.net": "fake-cloudflare-token-two"}
PAGE_SIZE = 2
"""Smaller than any test zone, so every listing spans pages."""


class FakeCloudflare:
    """Zones by name, records by zone id, and every request as `METHOD /path`."""

    def __init__(self) -> None:
        self.zones = {name: f"id-{name}" for name in TOKENS}
        self.records: dict[str, dict[str, dict[str, Any]]] = {i: {} for i in self.zones.values()}
        self.calls: list[str] = []
        self.rate_limited = 0
        self.broken: dict[str, tuple[int, dict[str, str], bytes]] = {}
        """Raw answers by `METHOD /path`: status, headers and body as sent."""
        self._next = 0

    def add(self, zone: str, name: str, comment: str = "", **fields: Any) -> str:
        """Add a record and return its id. A CNAME to `home.<zone>` unless `fields` say other."""
        self._next += 1
        record_id = f"r{self._next}"
        record = {"type": "CNAME", "content": f"home.{zone}", "proxied": False, "ttl": 1}
        record |= {"id": record_id, "name": name, "comment": comment or None, **fields}
        self.records[self.zones[zone]][record_id] = record
        return record_id

    def zone(self, zone: str) -> dict[str, dict[str, Any]]:
        """Return the records of a zone by id."""
        return self.records[self.zones[zone]]

    def writes(self) -> list[str]:
        """Return every call that is not a GET."""
        return [call for call in self.calls if not call.startswith("GET")]

    def handle(self, method: str, url: str, token: str, body: Any) -> tuple[int, Any]:
        """Answer one request like the real API does."""
        parts = urlsplit(url)
        query = parse_qs(parts.query)
        if parts.path == "/zones":
            name = query["name"][0]
            found = [{"id": self.zones[name], "name": name}] if name in self.zones else []
            return 200, ok(found)
        match = re.fullmatch(r"/zones/([^/]+)/dns_records(?:/([^/]+))?", parts.path)
        if match is None or match[1] not in self.records:
            return 404, {"success": False, "errors": [{"message": "not found"}]}
        zone = next(name for name, i in self.zones.items() if i == match[1])
        if token != f"Bearer {TOKENS[zone]}":
            return 403, {"success": False, "errors": [{"message": "Authentication error"}]}
        records = self.records[match[1]]
        if method == "GET":
            page = int(query["page"][0])
            items = list(records.values())
            pages = max(1, -(-len(items) // PAGE_SIZE))
            result = items[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]
            return 200, ok(result) | {"result_info": {"page": page, "total_pages": pages}}
        if method == "POST":
            record_id = self.add(zone, **body)
            return 200, ok(records[record_id])
        if method == "PUT":
            records[match[2]] |= body
            return 200, ok(records[match[2]])
        del records[match[2]]
        return 200, ok({"id": match[2]})


def ok(result: Any) -> dict[str, Any]:
    return {"success": True, "errors": [], "result": result}


@pytest.fixture
def cloudflare(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeCloudflare]:
    fake = FakeCloudflare()
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def _answer(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length)) if length else None
            call = f"{self.command} {urlsplit(self.path).path}"
            with lock:
                fake.calls.append(call)
                if call in fake.broken:
                    status, headers, raw = fake.broken[call]
                    self.send_response(status)
                    for name, value in headers.items():
                        self.send_header(name, value)
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                if fake.rate_limited:
                    fake.rate_limited -= 1
                    status, answer = 429, {"success": False, "errors": [{"message": "slow"}]}
                else:
                    token = self.headers.get("Authorization", "")
                    status, answer = fake.handle(self.command, self.path, token, body)
            raw = json.dumps(answer).encode()
            self.send_response(status)
            if status == 429:
                self.send_header("Retry-After", "0")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        do_GET = do_POST = do_PUT = do_DELETE = _answer

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True).start()
    monkeypatch.setenv("CLOUDFLARE_BASE_URL", f"http://127.0.0.1:{server.server_port}")
    yield fake
    server.shutdown()
    server.server_close()


def snapshot(store: Store) -> None:
    """Take a snapshot, as an apply does once HAProxy runs the config."""
    with store.lock() as held:
        Snapshots(store).create("test", held)


@pytest.fixture
def store(tmp_path: Path) -> Store:
    """The seeded example data, applied, with the integration on and two zones."""
    shutil.copytree(DATA, tmp_path / "data")
    applied = Store(tmp_path / "data")
    snapshot(applied)
    return applied
