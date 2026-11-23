"""`/logs/stream` and the stored logs on the real app. `tests/unit/logs/test_stream.py` covers the
stream's filters and timing, and `tests/unit/logs/test_logs_files.py` the files the API writes.

The test client waits for a response to end, and this one never does, so the request runs as
raw ASGI with the session cookie of a logged-in client.
"""

import asyncio
import json
from collections.abc import Callable, MutableMapping
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from homeproxy.logs import AccessLog, LogCollector
from tests.integration.api.conftest import make_client

Message = MutableMapping[str, Any]


def datagram(route: str) -> bytes:
    """An HTTP access line as HAProxy sends it to the collector."""
    return (
        f"<134>1 2026-11-09T17:08:10.587251+00:00 - haproxy 8 - - 192.168.1.50:60834 "
        f"[09/Nov/2026:17:08:10.586] fe_https~ route-{route}/192.168.1.20:8080 0/0/1/2/3 200 512 "
        f'- - ---- 2/1/0/0/0 0/0 "GET /{route} HTTP/1.1" "{route}" "DE" ""\n'
    ).encode()


async def events(
    api: TestClient, query: str, then: Callable[[], object] | None = None
) -> tuple[int, list[dict[str, Any]]]:
    """Open the stream and return its status and data events: the first one, or with `then`,
    every one until the stream ends after `then` ran."""
    cookie = "; ".join(f"{name}={value}" for name, value in api.cookies.items())
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": "/api/v1/logs/stream",
        "raw_path": b"/api/v1/logs/stream",
        "root_path": "",
        "query_string": query.encode(),
        "headers": [(b"host", b"testserver"), (b"cookie", cookie.encode())],
        "client": ("192.168.1.50", 50000),
        "server": ("testserver", 443),
    }
    sent: asyncio.Queue[Message] = asyncio.Queue()
    done = asyncio.Event()

    async def receive() -> Message:
        await done.wait()
        return {"type": "http.disconnect"}

    request = asyncio.ensure_future(api.app(scope, receive, sent.put))
    found: list[dict[str, Any]] = []
    try:
        async with asyncio.timeout(5):
            status = (await sent.get())["status"]
            while message := await sent.get():
                body = message.get("body", b"")
                if body.startswith(b"data: "):
                    found.append(json.loads(body.removeprefix(b"data: ")))
                    if then is None:
                        break
                    if len(found) == 1:
                        then()
                if not message.get("more_body", False):
                    break
    finally:
        done.set()
        async with asyncio.timeout(5):
            await request
    return status, found


def test_stream_sends_the_collected_lines_that_match(api: TestClient) -> None:
    """The endpoint serves the API's own collector, with the filters from the query."""
    collector: LogCollector = api.app.state.collector  # type: ignore[attr-defined]
    for route in ("app", "nas"):
        collector.receive(datagram(route))

    status, found = asyncio.run(events(api, "route=nas"))

    assert status == 200
    assert [(event["route"], event["request"]) for event in found] == [("nas", "GET /nas HTTP/1.1")]


def test_stream_ends_when_its_session_does(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tab left open on Logs must not keep receiving client addresses after a logout or a
    password change elsewhere. The stream stops listening to the collector too."""
    collector: LogCollector = api.app.state.collector  # type: ignore[attr-defined]
    collector.receive(datagram("app"))
    listening: list[object] = []
    subscribe = collector.subscribe

    def counted(listener: Callable[[AccessLog], None]) -> Callable[[], None]:
        listening.append(listener)
        unsubscribe = subscribe(listener)

        def stop() -> None:
            listening.remove(listener)
            unsubscribe()

        return stop

    monkeypatch.setattr(collector, "subscribe", counted)

    def sign_out_then_log() -> None:
        assert api.post("/api/v1/auth/logout").status_code == 204
        collector.receive(datagram("later"))

    status, found = asyncio.run(events(api, "", then=sign_out_then_log))

    assert status == 200
    assert [event["route"] for event in found] == ["app"]
    assert listening == []


def test_stored_logs_are_listed_and_each_source_downloads_oldest_first(
    data_dir: Path, api: TestClient
) -> None:
    """s6-log's archives sort by their TAI64N names, the API's own rotated files count down, and
    neither tool's bookkeeping files are part of the log."""
    logs = data_dir / "state" / "logs"
    (logs / "dns").mkdir(parents=True)
    (logs / "dns" / "@400000006b00d9ae236e9d8d.s").write_text("first\n")
    (logs / "dns" / "@400000006b00d9af00000000.s").write_text("second\n")
    (logs / "dns" / "current").write_text("third\n")
    (logs / "dns" / "lock").write_text("")
    (logs / "dns" / "state").write_text("kept by s6-log\n")
    (logs / "access").mkdir()
    for name, text in (("access.log.2", "a\n"), ("access.log.1", "b\n"), ("access.log", "c\n")):
        (logs / "access" / name).write_text(text)

    listed = api.get("/api/v1/logs/files").json()
    downloaded = api.get("/api/v1/logs/files/dns")

    assert {entry["source"]: entry["size"] for entry in listed} == {
        "access": 6,
        "haproxy": 0,
        "api": 0,
        "dns": 19,
        "spoa": 0,
    }
    assert next(entry for entry in listed if entry["source"] == "spoa")["modified"] is None
    assert downloaded.text == "first\nsecond\nthird\n"
    assert downloaded.headers["content-type"] == "text/plain; charset=utf-8"
    assert downloaded.headers["content-disposition"] == 'attachment; filename="homeproxy-dns.log"'
    assert api.get("/api/v1/logs/files/access").text == "a\nb\nc\n"


def test_stored_logs_need_a_session_and_a_known_source(data_dir: Path, api: TestClient) -> None:
    assert make_client(data_dir).get("/api/v1/logs/files/access").status_code == 401
    assert api.get("/api/v1/logs/files/secrets").status_code == 422
