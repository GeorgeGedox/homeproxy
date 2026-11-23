"""Confirm-or-revert for a new `dashboard.listen`, so a wrong address never locks the admin out.

1. The new address is bound at once, so an address this host cannot bind is refused.
2. A second server answers on it beside the current one, with the same app, certificate and
   source filter.
3. `config.yaml` still holds the old address. It gets the new one only when a request with a valid
   session reaches the new server within `CONFIRM_SECONDS`. The admin's cookie may not reach the
   new address, for example when its IP differs, so logging in there and then making any API call
   confirms too.
4. Without that, the new server closes at the deadline. A restart in the window also keeps the old
   address, because nothing was saved.

`CLOSE_DELAY` seconds after the confirmation, the old address stops listening, so a move that
narrows who can reach the dashboard takes effect at once.
"""

import asyncio
import logging
import socket
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import uvicorn
from starlette.concurrency import run_in_threadpool
from starlette.types import ASGIApp, Receive, Scope, Send

from homeproxy.auth.server import ListenPlan, build_config, split_listen
from homeproxy.auth.sources import SourcePolicy
from homeproxy.store import Store

logger = logging.getLogger(__name__)

CONFIRM_SECONDS = 60.0
"""How long a new address waits for an authenticated request before it closes."""

CLOSE_DELAY = 3.0
"""Seconds between a confirmation and the close of the old address, so requests the admin's page
already sent there still get their answers."""

WINDOW_SCOPE_KEY = "homeproxy.listen_window"
"""ASGI scope key that marks a request as one that arrived on the new address."""


class ListenRefused(Exception):
    """The address cannot be bound on this host. The text says why."""


@dataclass(frozen=True)
class PendingListen:
    """A new listen address that waits for its confirmation."""

    listen: str
    url: str
    expires_at: datetime


@dataclass(eq=False)
class _Window:
    pending: PendingListen
    save: Callable[[str], bool]
    marker: object
    server: uvicorn.Server
    sock: socket.socket
    # Held so the tasks are not garbage collected while they run.
    ticker: asyncio.Task[None] | None = None
    expiry: asyncio.Task[None] | None = None


def _bind(listen: str) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(split_listen(listen))
    except OSError as error:
        sock.close()
        raise ListenRefused(f"cannot listen on {listen}: {error.strerror or error}") from None
    return sock


def _tag(app: ASGIApp, marker: object) -> ASGIApp:
    """Return `app` with every request marked by `marker`, the window it arrived in."""

    async def tagged(scope: Scope, receive: Receive, send: Send) -> None:
        scope[WINDOW_SCOPE_KEY] = marker
        await app(scope, receive, send)

    return tagged


def _stop_listening(server: uvicorn.Server) -> None:
    """Close a server's listener and its connections. The process and its services keep running,
    so this also works on the server `homeproxy serve` started."""
    for listener in server.servers:
        listener.close()
    for connection in list(server.server_state.connections):
        connection.shutdown()


class ListenChange:
    """At most one new listen address waiting for confirmation in this process."""

    def __init__(
        self,
        store: Store,
        policy: Callable[[], SourcePolicy],
        confirm_seconds: float = CONFIRM_SECONDS,
    ) -> None:
        """Serve with the certificate in `store`. `policy` returns the `SourcePolicy` for each new
        connection. A new address closes `confirm_seconds` after it opens unless confirmed."""
        self.store = store
        self._policy = policy
        self._confirm_seconds = confirm_seconds
        self._lock = asyncio.Lock()
        self._window: _Window | None = None
        self.serving: uvicorn.Server | None = None
        """The server on the saved address. `homeproxy serve` sets it; a confirmation replaces
        it."""
        self._serving_window: _Window | None = None
        self._retiring: asyncio.Task[None] | None = None

    @property
    def pending(self) -> PendingListen | None:
        """The address that waits for its confirmation, if any."""
        window = self._window
        return None if window is None else window.pending

    async def begin(
        self, app: ASGIApp, listen: str, url: str, save: Callable[[str], bool]
    ) -> PendingListen:
        """Serve `app` on `listen` as well. A window already open is closed.

        `save(listen)` runs in a worker thread when a session confirms the address, and returns
        whether it saved it. Raises `ListenRefused` when the address cannot be bound.
        """
        old: _Window | None = None
        try:
            async with self._lock:
                old, self._window = self._window, None
                window = self._window = await self._open(app, listen, url, save)
        finally:
            if old is not None:
                await self._close(old, "replaced by another address")
        logger.info("dashboard also listening on %s for confirmation", listen)
        return window.pending

    async def cancel(self) -> None:
        """Close the waiting address, if any. The saved one stays."""
        async with self._lock:
            window, self._window = self._window, None
        if window is not None:
            await self._close(window, "cancelled")

    async def confirm(self, marker: object) -> None:
        """Save the address a request with a valid session arrived on, if it still waits. Then
        close the old address."""
        async with self._lock:
            window = self._window
            if window is None or marker is not window.marker:
                return
            if not await run_in_threadpool(window.save, window.pending.listen):
                return
            self._window = None
            old = (self.serving, self._serving_window)
            self.serving, self._serving_window = window.server, window
        if window.expiry is not None:
            window.expiry.cancel()
        logger.info("dashboard.listen confirmed and saved: %s", window.pending.listen)
        self._retiring = asyncio.create_task(self._retire(*old))

    async def _retire(self, server: uvicorn.Server | None, window: _Window | None) -> None:
        """Stop the old address after `CLOSE_DELAY`. A server this class started stops whole; the
        one `homeproxy serve` started only stops listening, because the process runs on it."""
        await asyncio.sleep(CLOSE_DELAY)
        if window is not None:
            window.server.should_exit = True
            await window.server.shutdown(sockets=[window.sock])
        elif server is not None:
            _stop_listening(server)
        logger.info("dashboard stopped listening on its old address")

    async def _open(
        self, app: ASGIApp, listen: str, url: str, save: Callable[[str], bool]
    ) -> _Window:
        sock = _bind(listen)
        expires = datetime.now(UTC) + timedelta(seconds=self._confirm_seconds)
        plan = ListenPlan(*split_listen(listen), setup_mode=False, reason="pending")
        certificate = str(self.store.root / "tls" / "dashboard.pem")
        marker = object()
        config = build_config(plan, certificate, _tag(app, marker), self._policy, "off")
        server = uvicorn.Server(config)
        window = _Window(PendingListen(listen, url, expires), save, marker, server, sock)
        try:
            config.load()
            server.lifespan = config.lifespan_class(config)
            await server.startup(sockets=[sock])
        except BaseException:
            sock.close()
            raise
        # main_loop keeps the Date header fresh and ends when should_exit is set.
        window.ticker = asyncio.create_task(server.main_loop())
        window.expiry = asyncio.create_task(self._expire(window))
        return window

    async def _expire(self, window: _Window) -> None:
        await asyncio.sleep(self._confirm_seconds)
        async with self._lock:
            if self._window is not window:
                return
            self._window = None
        await self._close(window, "not confirmed in time")

    async def _close(self, window: _Window, reason: str) -> None:
        window.server.should_exit = True
        await window.server.shutdown(sockets=[window.sock])
        logger.warning(
            "dashboard stopped listening on %s (%s); dashboard.listen is unchanged",
            window.pending.listen,
            reason,
        )
