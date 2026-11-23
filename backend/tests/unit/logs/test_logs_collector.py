"""The collector on a real Unix datagram socket, in a real event loop."""

import asyncio
import socket
import stat
from collections.abc import Awaitable, Callable
from ipaddress import IPv4Address, IPv6Address
from pathlib import Path

import pytest

from homeproxy.geoip.country_map import Ranges
from homeproxy.geoip.data import DATA_DIR
from homeproxy.geoip.lookup import CountryLookup, write_lookup
from homeproxy.logs import AccessLog, LogCollector
from homeproxy.util.redact import register_secret

LINE = (
    b"<134>1 2026-11-09T17:08:10.587251+00:00 - haproxy 8 - - "
    b"172.17.0.1:60834 [09/Nov/2026:17:08:10.586] fe_https~ route-app/192.168.1.20:8080 "
    b'0/0/1/2/3 200 512 - - ---- 2/1/0/0/0 0/0 "GET / HTTP/1.1" "app" "" ""\n'
)
NOTICE = b"<133>1 2026-11-09T17:08:10.587251+00:00 - haproxy 8 - - Proxy fe_https started.\n"


def send(path: Path, data: bytes) -> None:
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as client:
        client.sendto(data, str(path))


async def wait_until(condition: Callable[[], bool]) -> None:
    async with asyncio.timeout(5):
        while not condition():
            await asyncio.sleep(0.01)


def run_with_collector(
    path: Path, test: Callable[[LogCollector], Awaitable[None]], tail_length: int = 10
) -> None:
    async def main() -> None:
        collector = LogCollector(path, tail_length=tail_length)
        await collector.start()
        try:
            await test(collector)
        finally:
            collector.close()

    asyncio.run(main())


def test_receives_parses_and_passes_lines_to_listeners(tmp_path: Path) -> None:
    path = tmp_path / "syslog.sock"
    received: list[AccessLog] = []

    async def test(collector: LogCollector) -> None:
        collector.subscribe(received.append)
        send(path, NOTICE)
        send(path, LINE)
        await wait_until(lambda: len(received) == 1)
        assert collector.tail() == received
        assert received[0].route == "app"

    run_with_collector(path, test)


def test_tail_and_listeners_get_the_line_redacted(tmp_path: Path) -> None:
    """The live tail reaches the UI, so a secret in a request line is hidden there too."""
    register_secret("push-token-123")
    received: list[AccessLog] = []
    collector = LogCollector(tmp_path / "syslog.sock")
    collector.subscribe(received.append)

    collector.receive(LINE.replace(b"GET / ", b"GET /push/push-token-123 "))

    assert received[0].request == "GET /push/[REDACTED] HTTP/1.1"
    assert "push-token-123" not in received[0].line
    assert collector.tail() == received


def test_socket_is_private_and_removed_on_close(tmp_path: Path) -> None:
    path = tmp_path / "syslog.sock"

    async def test(collector: LogCollector) -> None:
        mode = path.lstat().st_mode
        assert stat.S_ISSOCK(mode)
        assert stat.S_IMODE(mode) == 0o600
        collector.close()
        assert not path.exists()
        collector.close()

    run_with_collector(path, test)


def test_stale_socket_is_replaced(tmp_path: Path) -> None:
    path = tmp_path / "syslog.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as old:
        old.bind(str(path))

    async def test(collector: LogCollector) -> None:
        send(path, LINE)
        await wait_until(lambda: len(collector.tail()) == 1)

    run_with_collector(path, test)


def test_other_file_at_the_path_is_refused_and_kept(tmp_path: Path) -> None:
    path = tmp_path / "syslog.sock"
    path.write_text("not a socket")

    async def main() -> None:
        with pytest.raises(FileExistsError):
            await LogCollector(path).start()

    asyncio.run(main())
    assert path.read_text() == "not a socket"


def test_second_start_is_refused(tmp_path: Path) -> None:
    async def test(collector: LogCollector) -> None:
        with pytest.raises(RuntimeError):
            await collector.start()

    run_with_collector(tmp_path / "syslog.sock", test)


def test_failing_listener_does_not_stop_the_others(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(entry: AccessLog) -> None:
        raise ValueError("broken listener")

    received: list[AccessLog] = []
    collector = LogCollector(tmp_path / "syslog.sock")
    collector.subscribe(broken)
    collector.subscribe(received.append)
    assert collector.receive(LINE) is not None
    assert len(received) == 1
    assert len(collector.tail()) == 1
    assert [r.getMessage() for r in caplog.records] == ["a log listener failed"]
    caplog.clear()


def test_unsubscribed_listener_gets_no_more_lines(tmp_path: Path) -> None:
    received: list[AccessLog] = []
    collector = LogCollector(tmp_path / "syslog.sock")
    unsubscribe = collector.subscribe(received.append)
    collector.receive(LINE)
    unsubscribe()
    unsubscribe()
    collector.receive(LINE)
    assert len(received) == 1


def test_tail_keeps_only_the_newest_lines(tmp_path: Path) -> None:
    collector = LogCollector(tmp_path / "syslog.sock", tail_length=3)
    lines = [LINE.replace(b"/ HTTP", f"/{index} HTTP".encode()) for index in range(5)]
    for line in lines:
        collector.receive(line)
    assert [entry.request for entry in collector.tail()] == [
        f"GET /{index} HTTP/1.1" for index in (2, 3, 4)
    ]


@pytest.mark.parametrize(
    ("client", "logged", "country"),
    [
        pytest.param(b"1.0.0.1", b"", "CN", id="not-blocked-found-by-the-collector"),
        # HAProxy blocked on its own map, which can differ from the lookup's, for example per /32.
        pytest.param(b"1.0.0.1", b"FR", "FR", id="blocked-logged-by-haproxy-wins"),
        pytest.param(b"2001:db8::1", b"", "FR", id="ipv6"),
        pytest.param(b"172.17.0.1", b"", None, id="no-country"),
    ],
)
def test_every_line_gets_its_clients_country(
    tmp_path: Path, client: bytes, logged: bytes, country: str | None
) -> None:
    """HAProxy logs only blocked countries. The collector finds the others."""
    (tmp_path / DATA_DIR).mkdir()
    ipv4, ipv6 = Ranges(32), Ranges(128)
    ipv4.add(int(IPv4Address("1.0.0.0")), int(IPv4Address("1.0.0.255")), "CN")
    ipv6.add(
        int(IPv6Address("2001:db8::")),
        int(IPv6Address("2001:db8:ffff:ffff:ffff:ffff:ffff:ffff")),
        "FR",
    )
    write_lookup(tmp_path / DATA_DIR, ipv4, ipv6)
    received: list[AccessLog] = []
    collector = LogCollector(tmp_path / "syslog.sock", countries=CountryLookup(tmp_path))
    collector.subscribe(received.append)

    line = LINE.replace(b"172.17.0.1", client).replace(b'"app" "" ""', b'"app" "%s" ""' % logged)
    collector.receive(line)

    assert [entry.country for entry in [*received, *collector.tail()]] == [country, country]
