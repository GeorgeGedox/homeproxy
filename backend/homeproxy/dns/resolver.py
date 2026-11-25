"""How the DNS server answers one query: local record, forward, refuse or drop.

| Query | Answer |
| --- | --- |
| A for a name in the table | The table's address, TTL 60, authoritative |
| Any other type for a name in the table | Empty NOERROR, so clients never prefer a public path |
| Any other name inside a zone | Forwarded to `forward_to`, the answer passed through |
| A name outside every zone, a zone transfer, an update | REFUSED |
| A client that is not private or is over its rate limit, a packet that is not a query | No answer |
"""

import ipaddress
import logging
import time
from collections.abc import Callable

import dns.asyncquery
import dns.entropy
import dns.exception
import dns.flags
import dns.message
import dns.opcode
import dns.rcode
import dns.rdataclass
import dns.rdatatype
import dns.rrset

from homeproxy.dns.snapshot import Snapshot
from homeproxy.models.domain import in_zone

RECORD_TTL = 60
RATE_LIMIT = 200
"""Queries per second per client. AdGuard sends every LAN client's queries, so it is generous."""
FORWARD_TIMEOUT = 2.0
_MAX_TRACKED_CLIENTS = 10_000
_REFUSED_TYPES = {dns.rdatatype.AXFR, dns.rdatatype.IXFR}

logger = logging.getLogger(__name__)


class ClientRateLimit:
    """A token bucket per client address: `rate` queries per second, with bursts of `rate`."""

    def __init__(self, rate: int, clock: Callable[[], float] = time.monotonic) -> None:
        """Start with every client's bucket full."""
        self._rate = rate
        self._clock = clock
        self._buckets: dict[str, tuple[float, float]] = {}

    def allow(self, client: str) -> bool:
        """Take one token for `client`, or return False when it has none left."""
        now = self._clock()
        tokens, last = self._buckets.get(client, (float(self._rate), now))
        tokens = min(float(self._rate), tokens + (now - last) * self._rate)
        if len(self._buckets) >= _MAX_TRACKED_CLIENTS:
            self._buckets.clear()
        allowed = tokens >= 1.0
        self._buckets[client] = (tokens - 1.0 if allowed else tokens, now)
        return allowed


class Resolver:
    """Answers queries from the current snapshot. Replace `snapshot` to reload."""

    def __init__(
        self,
        snapshot: Snapshot,
        *,
        rate_limit: ClientRateLimit | None = None,
        forward_port: int = 53,
    ) -> None:
        """Serve `snapshot`. `rate_limit` and `forward_port` keep their defaults outside tests."""
        self.snapshot = snapshot
        self._limit = rate_limit or ClientRateLimit(RATE_LIMIT)
        self._forward_port = forward_port

    async def handle(self, wire: bytes, client: str, *, tcp: bool) -> bytes | None:
        """Return the wire answer to one query from `client`, or None to send nothing."""
        # On a public listen address, the internet would learn the LAN address of every route.
        if not ipaddress.ip_address(client).is_private:
            return None
        if not self._limit.allow(client):
            return None
        try:
            request = dns.message.from_wire(wire)
        except dns.exception.DNSException:
            return None
        if request.flags & dns.flags.QR:
            return None
        rcode = self._refusal(request)
        if rcode is not None:
            return _reply(request, rcode)
        question = request.question[0]
        name = question.name.to_text(omit_final_dot=True).lower()
        address = self.snapshot.records.get(name)
        if address is not None:
            return _local_answer(request, address)
        return await self._forward(request, client, tcp=tcp)

    def _refusal(self, request: dns.message.Message) -> dns.rcode.Rcode | None:
        """Return the error code for a query this server does not answer, or None."""
        if request.opcode() != dns.opcode.QUERY:
            return dns.rcode.REFUSED
        if len(request.question) != 1:
            return dns.rcode.FORMERR
        question = request.question[0]
        if question.rdclass != dns.rdataclass.IN or question.rdtype in _REFUSED_TYPES:
            return dns.rcode.REFUSED
        name = question.name.to_text(omit_final_dot=True).lower()
        if not any(in_zone(name, zone) for zone in self.snapshot.zones):
            return dns.rcode.REFUSED
        return None

    async def _forward(self, request: dns.message.Message, client: str, *, tcp: bool) -> bytes:
        """Pass the query to each upstream in turn and return the first answer unchanged."""
        upstreams = self.snapshot.forward_to
        if client in upstreams:
            # The LAN resolver sends the zone here, so forwarding back to it would loop forever.
            logger.warning(
                "not forwarding: the client %s is also in forward_unknown_to. "
                "Remove your LAN resolver, such as AdGuard Home, from the DNS server's upstreams",
                client,
            )
            return _reply(request, dns.rcode.SERVFAIL)
        query = dns.message.from_wire(request.to_wire())
        query.id = dns.entropy.random_16()
        for upstream in upstreams:
            try:
                response = await self._ask(query, upstream, tcp=tcp)
            except (dns.exception.DNSException, OSError) as error:
                logger.info("upstream %s failed: %s", upstream, type(error).__name__)
                continue
            response.id = request.id
            return response.to_wire()
        return _reply(request, dns.rcode.SERVFAIL)

    async def _ask(
        self, query: dns.message.Message, upstream: str, *, tcp: bool
    ) -> dns.message.Message:
        """Send one query over the same transport the client used, so truncation passes through."""
        ask = dns.asyncquery.tcp if tcp else dns.asyncquery.udp
        return await ask(query, upstream, timeout=FORWARD_TIMEOUT, port=self._forward_port)


def _reply(request: dns.message.Message, rcode: dns.rcode.Rcode) -> bytes:
    response = dns.message.make_response(request)
    response.set_rcode(rcode)
    return response.to_wire()


def _local_answer(request: dns.message.Message, address: str) -> bytes:
    response = dns.message.make_response(request)
    response.flags |= dns.flags.AA | dns.flags.RA
    question = request.question[0]
    if question.rdtype == dns.rdatatype.A:
        response.answer.append(dns.rrset.from_text(question.name, RECORD_TTL, "IN", "A", address))
    return response.to_wire()
