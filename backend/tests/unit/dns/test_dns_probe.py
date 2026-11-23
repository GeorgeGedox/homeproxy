"""`dns_listening` and `dns_uptime_seconds`: what `/status` reports about the DNS server."""

import asyncio
from pathlib import Path

import dns.asyncquery
import dns.message

from homeproxy.dns.probe import dns_listening, dns_uptime_seconds
from homeproxy.dns.resolver import Resolver
from homeproxy.dns.server import DnsServer
from homeproxy.dns.service import run
from homeproxy.dns.snapshot import FILE_NAME, Snapshot


def test_the_server_counts_as_listening_until_it_closes(tmp_path: Path) -> None:
    path = tmp_path / FILE_NAME

    async def scenario() -> list[bool]:
        snapshot = Snapshot(listen="127.0.0.1:1", forward_to=[], zones=[], records={})
        server = DnsServer(Resolver(snapshot))
        port = await server.listen("127.0.0.1", 0)
        bound = snapshot.model_copy(update={"listen": f"127.0.0.1:{port}"})
        path.write_text(bound.model_dump_json())
        seen = [await asyncio.to_thread(dns_listening, path)]
        # An answer over TCP shows the server took the probe's connection too. Closing before
        # asyncio built its transport leaves a half-made one that raises when collected.
        query = dns.message.make_query("example.com", "A")
        await dns.asyncquery.tcp(query, "127.0.0.1", port=port, timeout=2)
        server.close()
        seen.append(await asyncio.to_thread(dns_listening, path))
        return seen

    assert asyncio.run(scenario()) == [True, False]


def test_without_a_snapshot_the_server_is_not_listening(tmp_path: Path) -> None:
    """A fresh install has no live release, so the DNS process has no address yet."""
    assert dns_listening(tmp_path / FILE_NAME) is False


def test_the_uptime_counts_from_the_process_start_until_it_stops(tmp_path: Path) -> None:
    pid_file = tmp_path / "dns.pid"

    async def scenario() -> list[int | None]:
        service = asyncio.create_task(run(tmp_path / FILE_NAME, pid_file))
        try:
            async with asyncio.timeout(5):
                while not pid_file.exists():
                    await asyncio.sleep(0.01)
            seen = [dns_uptime_seconds(pid_file)]
        finally:
            service.cancel()
            await asyncio.gather(service, return_exceptions=True)
        seen.append(dns_uptime_seconds(pid_file))
        return seen

    running, stopped = asyncio.run(scenario())
    assert running is not None and 0 <= running <= 5
    assert stopped is None
