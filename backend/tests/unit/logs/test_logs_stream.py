"""The live log tail, served the way the API serves it: FastAPI server-sent events."""

import asyncio
import json
import socket
from collections.abc import AsyncIterator, Callable, MutableMapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

import pytest
from fastapi import FastAPI, Query
from fastapi.sse import EventSourceResponse

from homeproxy.logs import AccessLog, LogCollector
from homeproxy.logs.stream import BACKLOG, QUEUE_LENGTH, LogFilter, follow

Message = MutableMapping[str, Any]


def line(
    path: str = "/",
    route: str = "app",
    status: int = 200,
    client: str = "192.168.1.10",
    country: str = "",
) -> bytes:
    return (
        f"<134>1 2026-11-09T17:08:10.587251+00:00 - haproxy 8 - - "
        f"{client}:60834 [09/Nov/2026:17:08:10.586] fe_https~ route-{route}/192.168.1.20:8080 "
        f'0/0/1/2/3 {status} 512 - - ---- 2/1/0/0/0 0/0 "GET {path} HTTP/1.1" '
        f'"{route}" "{country}" ""\n'
    ).encode()


@asynccontextmanager
async def sse_client(
    collector: LogCollector, query: str = ""
) -> AsyncIterator[asyncio.Queue[Message]]:
    """Run a request against the stream endpoint and hand over what it sends."""
    app = FastAPI()

    @app.get("/logs/stream", response_class=EventSourceResponse)
    async def stream(log_filter: Annotated[LogFilter, Query()]) -> AsyncIterator[AccessLog]:
        async for item in follow(collector, log_filter):
            yield item

    sent: asyncio.Queue[Message] = asyncio.Queue()
    disconnected = asyncio.Event()

    async def receive() -> Message:
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        await sent.put(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": "/logs/stream",
        "raw_path": b"/logs/stream",
        "root_path": "",
        "query_string": query.encode(),
        "headers": [],
        "client": ("192.168.1.10", 50000),
        "server": ("192.168.1.2", 8443),
    }
    request = asyncio.create_task(app(scope, receive, send))
    try:
        yield sent
    finally:
        disconnected.set()
        async with asyncio.timeout(5):
            await request


async def next_event(sent: asyncio.Queue[Message], timeout: float = 1) -> Message:
    """Return the JSON of the next `data:` event, or fail after `timeout` seconds."""
    async with asyncio.timeout(timeout):
        while True:
            message = await sent.get()
            body = message.get("body", b"")
            if body.startswith(b"data: "):
                result: Message = json.loads(body.removeprefix(b"data: "))
                return result


def test_new_line_reaches_the_client_within_one_second(tmp_path: Path) -> None:
    path = tmp_path / "syslog.sock"

    async def main() -> None:
        collector = LogCollector(path)
        await collector.start()
        try:
            async with sse_client(collector) as sent:
                start = await sent.get()
                assert start["status"] == 200
                assert (b"content-type", b"text/event-stream; charset=utf-8") in start["headers"]
                with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as haproxy:
                    haproxy.sendto(line(path="/new"), str(path))
                event = await next_event(sent, timeout=1)
                assert event["request"] == "GET /new HTTP/1.1"
                assert event["logged_at"] == "2026-11-09T17:08:10.587251Z"
        finally:
            collector.close()

    asyncio.run(main())


def test_stream_starts_with_kept_matching_lines_then_follows(tmp_path: Path) -> None:
    async def main() -> None:
        collector = LogCollector(tmp_path / "syslog.sock")
        for index in range(BACKLOG + 1):
            collector.receive(line(path=f"/{index}"))
        collector.receive(line(route="other"))
        async with sse_client(collector, "route=app") as sent:
            first = await next_event(sent)
            assert first["request"] == "GET /1 HTTP/1.1"
            for _ in range(BACKLOG - 1):
                await next_event(sent)
            collector.receive(line(path="/live"))
            assert (await next_event(sent))["request"] == "GET /live HTTP/1.1"

    asyncio.run(main())


@pytest.mark.parametrize(
    ("query", "matching", "other"),
    [
        ("route=app", {"route": "app"}, {"route": "other"}),
        ("status=503", {"status": 503}, {"status": 200}),
        ("client=192.168.1.10", {"client": "192.168.1.10"}, {"client": "unix"}),
        ("client=192.168.1.0/24", {"client": "192.168.1.10"}, {"client": "192.168.2.10"}),
        ("client=2001:db8::/32", {"client": "2001:db8::1"}, {"client": "192.168.1.10"}),
        ("country=de", {"country": "DE"}, {"country": ""}),
        ("route=app&status=404", {"route": "app", "status": 404}, {"route": "app"}),
    ],
)
def test_filter_sends_only_matching_lines(
    tmp_path: Path, query: str, matching: dict[str, Any], other: dict[str, Any]
) -> None:
    async def main() -> None:
        collector = LogCollector(tmp_path / "syslog.sock")
        async with sse_client(collector, query) as sent:
            await sent.get()
            # Lines go out in order, so a first event that is the match shows the other was dropped.
            collector.receive(line(path="/other", **other))
            collector.receive(line(path="/match", **matching))
            assert (await next_event(sent))["request"] == "GET /match HTTP/1.1"

    asyncio.run(main())


@pytest.mark.parametrize(
    ("query", "matching", "other"),
    [
        pytest.param("network=lan", "192.168.1.10", "203.0.113.9", id="lan-hides-the-internet"),
        pytest.param("network=internet", "203.0.113.9", "192.168.1.10", id="internet-hides-lan"),
    ],
)
def test_network_filter_tells_lan_from_internet_clients(
    tmp_path: Path, query: str, matching: str, other: str
) -> None:
    async def main() -> None:
        collector = LogCollector(tmp_path / "syslog.sock", lan=lambda ip: ip.startswith("192.168."))
        async with sse_client(collector, query) as sent:
            await sent.get()
            collector.receive(line(path="/other", client=other))
            collector.receive(line(path="/match", client=matching))
            assert (await next_event(sent))["request"] == "GET /match HTTP/1.1"

    asyncio.run(main())


@pytest.mark.parametrize(
    "query", ["status=99", "client=not-an-ip", "client=192.168.1.10/24", "network=moon"]
)
def test_bad_filter_is_refused(tmp_path: Path, query: str) -> None:
    async def main() -> None:
        collector = LogCollector(tmp_path / "syslog.sock")
        async with sse_client(collector, query) as sent:
            assert (await sent.get())["status"] == 422

    asyncio.run(main())


class CountingCollector(LogCollector):
    """Counts its listeners, because the collector does not show them."""

    listeners = 0

    def subscribe(self, listener: Callable[[AccessLog], None]) -> Callable[[], None]:
        unsubscribe = super().subscribe(listener)
        self.listeners += 1

        def counted() -> None:
            self.listeners -= 1
            unsubscribe()

        return counted


def test_disconnect_removes_the_listener(tmp_path: Path) -> None:
    async def main() -> None:
        collector = CountingCollector(tmp_path / "syslog.sock")
        async with sse_client(collector) as sent:
            collector.receive(line())
            await next_event(sent)
            assert collector.listeners == 1
        assert collector.listeners == 0

    asyncio.run(main())


def test_stalled_client_holds_a_bounded_number_of_lines(tmp_path: Path) -> None:
    async def main() -> None:
        collector = LogCollector(tmp_path / "syslog.sock")
        stream = follow(collector, LogFilter())
        reading = asyncio.ensure_future(anext(stream))
        await asyncio.sleep(0)
        for index in range(QUEUE_LENGTH + 10):
            collector.receive(line(path=f"/{index}"))
        received = [await reading]
        received += [await anext(stream) for _ in range(QUEUE_LENGTH - 1)]
        assert received[-1].request == f"GET /{QUEUE_LENGTH - 1} HTTP/1.1"
        collector.receive(line(path="/after"))
        assert (await anext(stream)).request == "GET /after HTTP/1.1"
        await stream.aclose()

    asyncio.run(main())
