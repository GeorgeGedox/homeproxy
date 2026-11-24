"""The live log tail: the newest matching access lines, then each new one as it arrives.

The API serves `follow()` as server-sent events with FastAPI's `EventSourceResponse`, which
encodes each `AccessLog` as JSON and sends keep-alive comments while no line matches.
"""

import asyncio
import ipaddress
from collections.abc import AsyncGenerator
from contextlib import suppress
from typing import Literal

from pydantic import ConfigDict, Field, IPvAnyNetwork

from homeproxy.logs.collector import LAN_COUNTRY, LogCollector
from homeproxy.logs.parser import AccessLog
from homeproxy.models.base import MODEL_CONFIG, StrictModel

BACKLOG = 200
"""How many of the kept lines a new stream starts with, so the page is not empty."""

QUEUE_LENGTH = 1000
"""Lines held for a client that reads too slowly. Newer lines are dropped until it catches up."""


class LogFilter(StrictModel):
    """Which access lines a stream sends. Every field that is set must match.

    A `StrictModel`, so a refused filter names its field. Query values are text, so it is lax.
    """

    model_config = MODEL_CONFIG | ConfigDict(strict=False)

    route: str | None = None
    status: int | None = Field(default=None, ge=100, le=599)
    client: IPvAnyNetwork | None = Field(
        default=None, description="A client address, or a range such as 192.168.1.0/24."
    )
    country: str | None = Field(default=None, description="A country code such as DE.")

    network: Literal["lan", "internet"] | None = Field(
        default=None, description="Only clients in `network.lan_cidrs`, or only the others."
    )

    def matches(self, entry: AccessLog) -> bool:
        """Tell whether `entry` passes every filter that is set."""
        if self.route is not None and entry.route != self.route:
            return False
        if self.status is not None and entry.status != self.status:
            return False
        if self.network is not None and (entry.country == LAN_COUNTRY) != (self.network == "lan"):
            return False
        if self.country is not None and entry.country != self.country.upper():
            return False
        return self.client is None or _in_network(entry.client_ip, self.client)


def _in_network(address: str, network: ipaddress.IPv4Network | ipaddress.IPv6Network) -> bool:
    try:
        return ipaddress.ip_address(address) in network
    except ValueError:
        return False


async def follow(collector: LogCollector, log_filter: LogFilter) -> AsyncGenerator[AccessLog]:
    """Yield the newest `BACKLOG` kept lines that match, then each new matching line.

    The stream runs until the caller stops iterating, which removes its listener.
    """
    queue: asyncio.Queue[AccessLog] = asyncio.Queue(QUEUE_LENGTH)

    def receive(entry: AccessLog) -> None:
        if log_filter.matches(entry):
            # A stalled client must not hold memory without a limit or block the collector.
            with suppress(asyncio.QueueFull):
                queue.put_nowait(entry)

    # Subscribing and reading the tail with no await between them gives no gap and no duplicate.
    unsubscribe = collector.subscribe(receive)
    try:
        for entry in [entry for entry in collector.tail() if log_filter.matches(entry)][-BACKLOG:]:
            yield entry
        while True:
            yield await queue.get()
    finally:
        unsubscribe()
