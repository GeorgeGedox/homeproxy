"""The supervised DNS process: read the snapshot, listen, and reload on SIGHUP.

It never imports the API, so a dashboard crash leaves DNS answering. A missing or invalid snapshot
keeps the table already loaded; with none loaded yet, the process waits for SIGHUP. To reload,
send SIGHUP to the pid in `PID_FILE`. The API runs as the same user, so it may.
"""

import asyncio
import contextlib
import logging
import os
import signal
from pathlib import Path

import pydantic

from homeproxy.dns.resolver import Resolver
from homeproxy.dns.server import DnsServer
from homeproxy.dns.snapshot import FILE_NAME, Snapshot, load_snapshot
from homeproxy.models import safe_errors
from homeproxy.util.atomic import atomic_write

SNAPSHOT_PATH = Path("/data/generated") / FILE_NAME
PID_FILE = Path("/run/homeproxy/dns.pid")
BIND_RETRY_SECONDS = 30.0

logger = logging.getLogger(__name__)


def read_snapshot(path: Path) -> Snapshot | None:
    """Return the snapshot at `path`, or None after logging why it cannot be used."""
    try:
        return load_snapshot(path)
    except FileNotFoundError:
        logger.info("no %s yet; waiting for an apply", path)
    except OSError as error:
        logger.error("cannot read %s: %s", path, error)
    except pydantic.ValidationError as error:
        logger.error("invalid %s: %s", path, safe_errors(error))
    return None


class DnsService:
    """Keeps one listener bound to the snapshot's address and the resolver's table current."""

    def __init__(self) -> None:
        """Start with no table and no listener."""
        self._resolver: Resolver | None = None
        self._server: DnsServer | None = None
        self._bound: str | None = None

    @property
    def waiting_to_bind(self) -> bool:
        """True when a table is loaded but its address could not be bound."""
        return self._resolver is not None and self._server is None

    async def apply(self, snapshot: Snapshot) -> None:
        """Serve `snapshot`, moving the listener only when its address changed."""
        if self._resolver is None:
            self._resolver = Resolver(snapshot)
        self._resolver.snapshot = snapshot
        if snapshot.listen == self._bound:
            return
        self.close()
        host, _, port = snapshot.listen.rpartition(":")
        server = DnsServer(self._resolver)
        try:
            await server.listen(host, int(port))
        except OSError as error:
            logger.error("cannot listen on %s: %s", snapshot.listen, error)
            return
        self._server, self._bound = server, snapshot.listen
        logger.info("listening on %s for %s", snapshot.listen, ", ".join(snapshot.zones))

    def close(self) -> None:
        """Stop the listener, keeping the table."""
        if self._server is not None:
            self._server.close()
        self._server, self._bound = None, None


async def run(snapshot_path: Path = SNAPSHOT_PATH, pid_file: Path = PID_FILE) -> None:
    """Serve until SIGTERM or SIGINT, rereading `snapshot_path` on each SIGHUP."""
    loop = asyncio.get_running_loop()
    wake = asyncio.Event()
    stopping = False

    def stop() -> None:
        nonlocal stopping
        stopping = True
        wake.set()

    loop.add_signal_handler(signal.SIGHUP, wake.set)
    loop.add_signal_handler(signal.SIGTERM, stop)
    loop.add_signal_handler(signal.SIGINT, stop)
    # Written only here: `/status` reads its mtime as the process start time.
    atomic_write(pid_file, f"{os.getpid()}\n".encode())
    service = DnsService()
    try:
        while not stopping:
            wake.clear()
            snapshot = read_snapshot(snapshot_path)
            if snapshot is not None:
                await service.apply(snapshot)
            # A failed bind is retried, for example when the LAN address was not up yet.
            retry = BIND_RETRY_SECONDS if service.waiting_to_bind else None
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(wake.wait(), retry)
    finally:
        service.close()
        pid_file.unlink(missing_ok=True)


def main() -> None:
    """Entry point of the `dns` service."""
    logging.basicConfig(level=logging.INFO, format="dns: %(levelname)s %(message)s")
    asyncio.run(run())
