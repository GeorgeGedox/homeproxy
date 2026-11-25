"""The log collector: HAProxy sends each access line to a Unix datagram socket that the API owns.

The collector parses each line once, keeps the newest lines for the live tail and passes every
line to its listeners, such as the metrics counters and the stored access log. HAProxy logs a
country only when it is blocked somewhere, so the collector adds the country of the other access
lines from the full GeoIP data. A client in `network.lan_cidrs` gets the country `LAN` instead, so
the stream and the metrics tell it apart from an address with no country. Lines that are not
access lines, such as HAProxy's notice that a server went down, go to the `other` callback, which
stores them in the `haproxy` log.

HAProxy never waits for the collector: a datagram socket with a full queue, or with no reader,
makes HAProxy drop the line and keep serving.
"""

import asyncio
import logging
import os
import socket
import stat
from collections import deque
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from homeproxy.geoip.lookup import CountryLookup
from homeproxy.logs.parser import AccessLog, parse_message, parse_syslog
from homeproxy.util.redact import redact

SYSLOG_SOCKET = "/run/homeproxy/syslog.sock"
"""Where HAProxy sends its access lines. The generated `global` section names the same path."""

LAN_COUNTRY = "LAN"
"""The country of a client in `network.lan_cidrs`. It never clashes with a country code."""

TAIL_LENGTH = 5000
"""How many of the newest access lines the collector keeps for the live tail."""

_RECEIVE_BUFFER = 4 * 1024 * 1024
"""Room for bursts while the event loop is busy. Linux may cap it at `net.core.rmem_max`."""

logger = logging.getLogger(__name__)

Listener = Callable[[AccessLog], None]
"""Called in the event loop with each access line. It must return quickly and not block."""

OtherLines = Callable[[datetime, str], None]
"""Called in the event loop with the time and message of each line that is not an access line. It
must return quickly, not block and not raise."""


class _Protocol(asyncio.DatagramProtocol):
    def __init__(self, receive: Callable[[bytes], object]) -> None:
        self._receive = receive

    def datagram_received(self, data: bytes, addr: object) -> None:
        self._receive(data)

    def error_received(self, exc: Exception) -> None:
        logger.warning("log collector socket error: %s", type(exc).__name__)


def _remove_stale_socket(path: Path) -> None:
    """A socket file outlives the process that bound it. Anything else at the path is a mistake."""
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(mode):
        raise FileExistsError(f"{path} exists and is not a socket")
    path.unlink()


def _bind(path: Path) -> socket.socket:
    _remove_stale_socket(path)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, _RECEIVE_BUFFER)
        sock.bind(str(path))
        # HAProxy runs as the same user as the API. Nobody else may add lines to the metrics.
        os.chmod(path, 0o600)
        sock.setblocking(False)
    except BaseException:
        sock.close()
        raise
    return sock


class LogCollector:
    """Receive HAProxy's syslog datagrams on a Unix socket and parse each one once.

    Start it in the API's event loop with `start()` and stop it with `close()`. Lines that are not
    access lines, such as a notice that a server went down, are counted and passed to `other`.
    """

    def __init__(
        self,
        socket_path: Path | None = None,
        tail_length: int = TAIL_LENGTH,
        countries: CountryLookup | None = None,
        lan: Callable[[str], bool] | None = None,
        other: OtherLines | None = None,
    ) -> None:
        """Prepare a collector on `socket_path`, which defaults to `SYSLOG_SOCKET`. A line that
        HAProxy logged with no country gets its country from `countries`. A client for which `lan`
        is true gets `LAN_COUNTRY`, whatever HAProxy logged. `lan` takes the address text and is
        called for each line, so it reads the current `network.lan_cidrs`."""
        self.socket_path = socket_path if socket_path is not None else Path(SYSLOG_SOCKET)
        self.countries = countries
        self._lan = lan
        self._lan_problem = ""
        self._other = other
        self._tail: deque[AccessLog] = deque(maxlen=tail_length)
        self._listeners: list[Listener] = []
        self._transport: asyncio.DatagramTransport | None = None
        self._inode: int | None = None

    async def start(self) -> None:
        """Bind the socket, replacing a stale one, and start receiving.

        Raises `FileExistsError` if something that is not a socket is at the path.
        """
        if self._transport is not None:
            raise RuntimeError("the log collector is already running")
        sock = _bind(self.socket_path)
        self._inode = self.socket_path.lstat().st_ino
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            lambda: _Protocol(self.receive), sock=sock
        )
        self._transport = transport

    def close(self) -> None:
        """Stop receiving and remove the socket, unless a newer collector has replaced it."""
        if self._transport is None:
            return
        self._transport.close()
        self._transport = None
        try:
            if self.socket_path.lstat().st_ino == self._inode:
                self.socket_path.unlink()
        except FileNotFoundError:
            pass

    def receive(self, data: bytes) -> AccessLog | None:
        """Handle one datagram: parse it, add its country, keep it in the tail and pass it to every
        listener."""
        syslog = parse_syslog(data)
        # The tail and the stream reach the UI, so they get the line redacted, as the file does.
        entry = None if syslog is None else parse_message(redact(syslog[1]), syslog[0])
        if entry is None:
            if syslog is not None and self._other is not None:
                self._other(*syslog)
            return None
        if self._is_lan(entry.client_ip):
            entry = replace(entry, country=LAN_COUNTRY)
        # HAProxy's country is the one it blocked on. The lookup can differ from it per /64.
        elif entry.country is None and self.countries is not None:
            country = self.countries.country(entry.client_ip)
            if country is not None:
                entry = replace(entry, country=country)
        self._tail.append(entry)
        for listener in tuple(self._listeners):
            try:
                listener(entry)
            except Exception:
                # One broken listener must not stop the tail or the others.
                logger.exception("a log listener failed")
        return entry

    def _is_lan(self, address: str) -> bool:
        """Ask the `lan` predicate. A failure counts as not LAN, so the line is never lost."""
        if self._lan is None:
            return False
        try:
            lan = self._lan(address)
        except Exception as error:  # the predicate reads files, so any failure must not stop a line
            self._report(type(error).__name__)
            return False
        self._report("")
        return lan

    def _report(self, problem: str) -> None:
        """Log a LAN check problem once when it starts, and once when it ends."""
        if problem == self._lan_problem:
            return
        if problem:
            logger.warning("cannot tell LAN clients, the country lookup is used: %s", problem)
        else:
            logger.info("LAN clients are told apart again")
        self._lan_problem = problem

    def tail(self) -> list[AccessLog]:
        """Return the kept access lines, oldest first."""
        return list(self._tail)

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        """Call `listener` with each new access line. Return a function that unsubscribes it."""
        self._listeners.append(listener)

        def unsubscribe() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return unsubscribe
