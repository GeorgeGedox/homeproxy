"""Answers of the DNS server over real sockets, queried with dnspython.

The server under test listens on 127.0.0.1. Its upstream is a second server on 127.0.0.2, which
stands in for a public resolver, so a forwarded name is answered with a public address.
"""

import asyncio
import threading
from collections.abc import Callable, Iterator

import dns.exception
import dns.flags
import dns.message
import dns.name
import dns.query
import dns.rcode
import dns.update
import pytest

from homeproxy.dns.resolver import RECORD_TTL, ClientRateLimit, Resolver
from homeproxy.dns.server import DnsServer
from homeproxy.dns.snapshot import Snapshot

LAN_IP = "192.168.1.10"
PUBLIC_IP = "203.0.113.7"
UPSTREAM = "127.0.0.2"
TIMEOUT = 2.0

Query = Callable[..., dns.message.Message]


@pytest.fixture
def loop() -> Iterator[asyncio.AbstractEventLoop]:
    """An event loop in a thread, so tests can query the servers with blocking calls."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    thread.join(TIMEOUT)
    loop.close()


def start(loop: asyncio.AbstractEventLoop, resolver: Resolver, host: str) -> tuple[DnsServer, int]:
    server = DnsServer(resolver)
    port = asyncio.run_coroutine_threadsafe(server.listen(host, 0), loop).result(TIMEOUT)
    return server, port


@pytest.fixture
def make_server(loop: asyncio.AbstractEventLoop) -> Iterator[Callable[[ClientRateLimit], Query]]:
    """Start the server under test with a rate limit, and return a function that queries it."""
    servers: list[DnsServer] = []
    public = Snapshot(
        listen=f"{UPSTREAM}:53",
        forward_to=[],
        zones=["example.com"],
        records={"blog.example.com": PUBLIC_IP},
    )
    upstream, upstream_port = start(loop, Resolver(public), UPSTREAM)
    servers.append(upstream)

    def make(rate_limit: ClientRateLimit) -> Query:
        snapshot = Snapshot(
            listen="127.0.0.1:53",
            forward_to=[UPSTREAM],
            zones=["example.com", "example.net"],
            records={"jellyfin.example.com": LAN_IP},
        )
        resolver = Resolver(snapshot, rate_limit=rate_limit, forward_port=upstream_port)
        server, port = start(loop, resolver, "127.0.0.1")
        servers.append(server)

        def query(
            message: dns.message.Message,
            *,
            tcp: bool = False,
            source: str | None = None,
            timeout: float = TIMEOUT,
        ) -> dns.message.Message:
            send = dns.query.tcp if tcp else dns.query.udp
            return send(message, "127.0.0.1", timeout=timeout, port=port, source=source)

        return query

    yield make

    async def close() -> None:
        for server in servers:
            server.close()
        # Let the closed sessions end on the loop, so no handler outlives it.
        await asyncio.gather(*asyncio.all_tasks() - {asyncio.current_task()})

    asyncio.run_coroutine_threadsafe(close(), loop).result(TIMEOUT)


@pytest.fixture
def query(make_server: Callable[[ClientRateLimit], Query]) -> Query:
    return make_server(ClientRateLimit(100))


@pytest.mark.parametrize("name", ["jellyfin.example.com", "JellyFin.Example.COM"])
def test_route_host_answers_its_lan_address(query: Query, name: str) -> None:
    response = query(dns.message.make_query(name, "A"))

    assert response.rcode() == dns.rcode.NOERROR
    assert response.flags & dns.flags.AA
    [rrset] = response.answer
    assert rrset.name == dns.name.from_text(name)
    assert rrset.ttl == RECORD_TTL
    assert [rdata.to_text() for rdata in rrset] == [LAN_IP]


def test_aaaa_for_route_host_is_empty(query: Query) -> None:
    response = query(dns.message.make_query("jellyfin.example.com", "AAAA"))

    assert response.rcode() == dns.rcode.NOERROR
    assert response.answer == []


@pytest.mark.parametrize("tcp", [False, True], ids=["udp", "tcp"])
def test_other_name_in_zone_is_forwarded(query: Query, tcp: bool) -> None:
    response = query(dns.message.make_query("blog.example.com", "A"), tcp=tcp)

    assert response.rcode() == dns.rcode.NOERROR
    assert [rdata.to_text() for rdata in response.answer[0]] == [PUBLIC_IP]


@pytest.mark.parametrize(
    "message",
    [
        dns.message.make_query("jellyfin.example.org", "A"),
        dns.message.make_query("example.com", "AXFR"),
        dns.update.UpdateMessage("example.com"),
    ],
    ids=["outside-zones", "zone-transfer", "update"],
)
def test_query_that_is_not_served_is_refused(query: Query, message: dns.message.Message) -> None:
    assert query(message).rcode() == dns.rcode.REFUSED


def test_query_from_a_forward_target_is_not_forwarded_back(query: Query) -> None:
    # The upstream here plays AdGuard: it is a forward target and sends queries too.
    response = query(dns.message.make_query("blog.example.com", "A"), source=UPSTREAM)

    assert response.rcode() == dns.rcode.SERVFAIL
    assert response.answer == []


@pytest.mark.parametrize(("client", "answered"), [("192.168.1.50", True), ("9.9.9.9", False)])
def test_only_a_private_client_gets_an_answer(client: str, answered: bool) -> None:
    """A listen address on the WAN must not tell the internet the LAN's addresses."""
    snapshot = Snapshot(
        listen="0.0.0.0:5335",
        forward_to=[],
        zones=["example.com"],
        records={"nas.example.com": LAN_IP},
    )
    wire = dns.message.make_query("nas.example.com", "A").to_wire()

    reply = asyncio.run(Resolver(snapshot).handle(wire, client, tcp=False))

    assert (reply is not None) == answered


def test_client_over_its_rate_limit_gets_no_answer_until_its_bucket_refills(
    make_server: Callable[[ClientRateLimit], Query],
) -> None:
    now = [0.0]
    query = make_server(ClientRateLimit(3, clock=lambda: now[0]))
    message = dns.message.make_query("jellyfin.example.com", "A")
    for _ in range(3):
        query(message)

    with pytest.raises(dns.exception.Timeout):
        # No answer is coming, so a short wait shows it as well as a long one.
        query(message, timeout=0.3)
    now[0] += 1.0
    assert query(message).rcode() == dns.rcode.NOERROR
