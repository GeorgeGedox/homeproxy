"""UDP and TCP listeners that pass each query to a `Resolver`."""

import asyncio
import logging
from typing import cast

from homeproxy.dns.resolver import Resolver

TCP_IDLE_TIMEOUT = 10.0

logger = logging.getLogger(__name__)


class _UdpProtocol(asyncio.DatagramProtocol):
    def __init__(self, resolver: Resolver) -> None:
        self._resolver = resolver
        self._transport: asyncio.DatagramTransport | None = None
        # The loop keeps only weak references to tasks, so running answers are held here.
        self._tasks: set[asyncio.Task[None]] = set()

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = cast(asyncio.DatagramTransport, transport)

    def datagram_received(self, data: bytes, addr: tuple[str | object, int]) -> None:
        task = asyncio.get_running_loop().create_task(self._answer(data, addr))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _answer(self, data: bytes, addr: tuple[str | object, int]) -> None:
        try:
            answer = await self._resolver.handle(data, str(addr[0]), tcp=False)
        except Exception:
            logger.exception("query from %s failed", addr[0])
            return
        if answer is not None and self._transport is not None:
            self._transport.sendto(answer, addr)


class DnsServer:
    """Serves one resolver on one address over UDP and TCP."""

    def __init__(self, resolver: Resolver) -> None:
        """Create a server that is not listening yet."""
        self._resolver = resolver
        self._udp: asyncio.DatagramTransport | None = None
        self._tcp: asyncio.Server | None = None

    async def listen(self, host: str, port: int) -> int:
        """Bind UDP and TCP on `host:port` and return the port. Port 0 picks a free one."""
        loop = asyncio.get_running_loop()
        self._tcp = await asyncio.start_server(self._serve_tcp, host, port)
        port = self._tcp.sockets[0].getsockname()[1]
        try:
            self._udp, _ = await loop.create_datagram_endpoint(
                lambda: _UdpProtocol(self._resolver), local_addr=(host, port)
            )
        except OSError:
            self.close()
            raise
        return port

    def close(self) -> None:
        """Stop listening and close open TCP sessions. Queries in progress may still finish."""
        if self._udp is not None:
            self._udp.close()
            self._udp = None
        if self._tcp is not None:
            self._tcp.close()
            # Open sessions end too, or their handlers would outlive the server.
            self._tcp.close_clients()
            self._tcp = None

    async def _serve_tcp(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        client = str(writer.get_extra_info("peername")[0])
        try:
            while True:
                async with asyncio.timeout(TCP_IDLE_TIMEOUT):
                    size = int.from_bytes(await reader.readexactly(2))
                    data = await reader.readexactly(size)
                answer = await self._resolver.handle(data, client, tcp=True)
                if answer is None:
                    break
                writer.write(len(answer).to_bytes(2) + answer)
                await writer.drain()
        except (TimeoutError, asyncio.IncompleteReadError, ConnectionError):
            pass  # an idle or closed connection is the normal end of a TCP session
        except Exception:
            logger.exception("TCP query from %s failed", client)
        finally:
            writer.close()
