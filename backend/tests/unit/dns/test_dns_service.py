"""The DNS process's reload: `run` rereads its snapshot when `reload_dns` signals it."""

import asyncio
import logging
import time
from pathlib import Path

import dns.asyncquery
import dns.exception
import dns.message
import pytest

from homeproxy.dns.service import run
from homeproxy.dns.signal import reload_dns
from homeproxy.dns.snapshot import FILE_NAME, Snapshot
from tests.conftest import free_port

LAN_IP = "192.168.1.10"
TIMEOUT = 5.0


async def answer(name: str, port: int) -> list[str]:
    """Query until the process answers, and return the A addresses."""
    deadline = time.monotonic() + TIMEOUT
    while True:
        try:
            response = await dns.asyncquery.udp(
                dns.message.make_query(name, "A"), "127.0.0.1", port=port, timeout=0.2
            )
        except dns.exception.Timeout:
            if time.monotonic() > deadline:
                raise
            continue
        return [rdata.to_text() for rrset in response.answer for rdata in rrset]


async def logged(caplog: pytest.LogCaptureFixture, text: str) -> None:
    deadline = time.monotonic() + TIMEOUT
    while not any(text in record.getMessage() for record in caplog.records):
        assert time.monotonic() < deadline, f"no log line with {text!r}"
        await asyncio.sleep(0.05)


def test_invalid_snapshot_on_reload_keeps_the_loaded_table(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    port = free_port()
    snapshot_path, pid_file = tmp_path / FILE_NAME, tmp_path / "dns.pid"
    snapshot = Snapshot(
        listen=f"127.0.0.1:{port}",
        forward_to=[],
        zones=["example.com"],
        records={"app.example.com": LAN_IP},
    )
    snapshot_path.write_text(snapshot.model_dump_json())
    caplog.set_level(logging.ERROR, logger="homeproxy.dns.service")

    async def scenario() -> None:
        service = asyncio.create_task(run(snapshot_path, pid_file))
        try:
            assert await answer("app.example.com", port) == [LAN_IP]
            snapshot_path.write_text('{"version": 1, "records": "broken"}')

            reload_dns(pid_file)
            await logged(caplog, f"invalid {snapshot_path}")

            assert await answer("app.example.com", port) == [LAN_IP]
        finally:
            service.cancel()
            await asyncio.gather(service, return_exceptions=True)

    asyncio.run(scenario())
