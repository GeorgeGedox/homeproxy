"""The dashboard server: HTTPS on the right address, and no answer to clients outside the LAN.

Where it listens:

- Setup mode (no `config.yaml`): `HOMEPROXY_BOOTSTRAP_LISTEN` if set, otherwise port 8443 on the
  address of the interface that holds the default route.
- After setup: `dashboard.listen`.
- `dashboard.listen` cannot be bound, for example because the host's LAN IP changed: the
  setup-mode address, private ranges only, so the admin can still log in and move it. If that
  cannot be bound either, every address on its port.
- `config.yaml` exists but its dashboard settings are unusable: the setup-mode address, so the
  admin can still reach the dashboard. Login reports the error.

The listen address is chosen at start. A change from the dashboard is tried on a second server
first (`homeproxy.auth.listen`) and used alone from the next start. Who may connect is decided per
connection by `AuthRuntime.source_policy`: `network.lan_cidrs` as the file says now, or private
ranges while there is no usable config. The fallback address always takes private ranges. A
connection from any other address is closed before a request is read, so the client gets no HTTP
response. With TLS the handshake finishes first, because asyncio hands the connection to the HTTP
protocol only after it.
"""

import asyncio
import importlib.metadata
import logging
import os
import socket
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

import uvicorn
from pydantic import TypeAdapter, ValidationError
from uvicorn.protocols.http.h11_impl import H11Protocol

from homeproxy.auth.dashboard import DashboardConfigError, read_dashboard_settings
from homeproxy.auth.setup_token import announce
from homeproxy.auth.sources import SourcePolicy, private_policy
from homeproxy.auth.tls import ensure_certificate, fingerprint
from homeproxy.models.fields import ListenAddress
from homeproxy.store import StoreError, copy_for_upgrade, migrate_files, prune_backups
from homeproxy.store.files import Store

logger = logging.getLogger(__name__)

BOOTSTRAP_ENV = "HOMEPROXY_BOOTSTRAP_LISTEN"
BOOTSTRAP_PORT = 8443
GRACEFUL_SHUTDOWN_SECONDS = 5

# TEST-NET-1. Connecting a UDP socket sends no packet. It only asks the kernel which source
# address it would use, which is the address of the default route's interface.
_PROBE_TARGET = ("192.0.2.1", 9)
_LISTEN: TypeAdapter[str] = TypeAdapter(ListenAddress)


class ServerConfigError(Exception):
    """The server cannot start with the given settings. The text says what to fix."""


@dataclass(frozen=True)
class ListenPlan:
    """Where the dashboard listens."""

    host: str
    port: int
    setup_mode: bool
    reason: str


def default_route_address() -> str | None:
    """Return the IPv4 address of the default route's interface, or None without a route."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(_PROBE_TARGET)
            address = str(probe.getsockname()[0])
    except OSError:
        return None
    return None if address == "0.0.0.0" else address  # noqa: S104 - compared, not bound


def split_listen(listen: str) -> tuple[str, int]:
    """Return the host and the port of an `ip:port` listen address."""
    host, _, port = listen.rpartition(":")
    return host, int(port)


def bootstrap_listen(environ: Mapping[str, str]) -> tuple[str, int]:
    """Return the setup-mode address: the variable, else the default route's address."""
    value = environ.get(BOOTSTRAP_ENV, "").strip()
    if value:
        failed = False
        try:
            _LISTEN.validate_python(value)
        except ValidationError:
            failed = True
        if failed:
            raise ServerConfigError(f"{BOOTSTRAP_ENV} must be an IPv4 address and port, ip:port")
        return split_listen(value)
    address = default_route_address()
    if address is None:
        logger.warning("no default route found; listening on every address, private ranges only")
        return "0.0.0.0", BOOTSTRAP_PORT  # noqa: S104 - only private sources are accepted
    return address, BOOTSTRAP_PORT


def bind_error(host: str, port: int) -> OSError | None:
    """Return why `host:port` cannot be bound now, or None if it can."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError as error:
            return error
    return None


def saved_listen(listen: str, environ: Mapping[str, str]) -> ListenPlan:
    """Plan `dashboard.listen`, or the setup-mode address while it cannot be bound."""
    host, port = split_listen(listen)
    error = bind_error(host, port)
    if error is None:
        return ListenPlan(host, port, False, "dashboard.listen")
    logger.error(
        "cannot listen on dashboard.listen %s: %s; using the setup address until a restart",
        listen,
        error.strerror or error,
    )
    host, port = bootstrap_listen(environ)
    error = bind_error(host, port)
    if error is not None:
        logger.error(
            "cannot listen on the setup address %s:%s either: %s; listening on every address",
            host,
            port,
            error.strerror or error,
        )
        host = "0.0.0.0"  # noqa: S104 - only private sources are accepted
    return ListenPlan(host, port, False, "fallback")


def plan_listen(store: Store, environ: Mapping[str, str]) -> ListenPlan:
    """Decide the listen address from `config.yaml` and the environment."""
    if store.config_path.exists():
        try:
            settings = read_dashboard_settings(store.config_path)
        except DashboardConfigError as error:
            logger.error("cannot use the dashboard settings: %s", error)
        except FileNotFoundError:
            pass
        else:
            return saved_listen(settings.listen, environ)
        if store.config_path.exists():
            host, port = bootstrap_listen(environ)
            return ListenPlan(host, port, False, "recovery")
    host, port = bootstrap_listen(environ)
    return ListenPlan(host, port, True, "setup")


def filtered_protocol(policy: Callable[[], SourcePolicy]) -> type[H11Protocol]:
    """Return uvicorn's HTTP protocol, closing connections from addresses outside `policy()`.

    `policy` is called for each new connection.
    """

    class SourceFilteredProtocol(H11Protocol):
        """H11Protocol that aborts the connection before reading a request from a bad source."""

        refused = False

        def connection_made(self, transport: asyncio.Transport) -> None:  # type: ignore[override]
            peer: Any = transport.get_extra_info("peername")
            host = str(peer[0]) if isinstance(peer, tuple) and peer else None
            allowed = policy()
            if not allowed.allows(host):
                self.refused = True
                logger.debug("refused connection from %s: not in %s", host, allowed.label)
                transport.abort()
                return
            super().connection_made(transport)

        def data_received(self, data: bytes) -> None:
            if self.refused:
                return
            super().data_received(data)

        def eof_received(self) -> None:
            if self.refused:
                return
            super().eof_received()

        def connection_lost(self, exc: Exception | None) -> None:
            if self.refused:
                return
            super().connection_lost(exc)

    return SourceFilteredProtocol


def build_config(
    plan: ListenPlan,
    certificate: str,
    app: Any,
    policy: Callable[[], SourcePolicy],
    lifespan: Literal["auto", "off"] = "auto",
) -> uvicorn.Config:
    """Return the uvicorn settings: TLS, one process, no proxy headers, filtered sources.

    A second server for the same app, such as a new listen address on trial, passes
    `lifespan="off"`, so the app's services start only once.
    """
    return uvicorn.Config(
        app,
        lifespan=lifespan,
        host=plan.host,
        port=plan.port,
        ssl_certfile=certificate,
        http=filtered_protocol(policy),
        ws="none",
        # The client address must come from the socket, or the throttle could be bypassed.
        proxy_headers=False,
        server_header=False,
        # No page on another port of this host may frame the dashboard (clickjacking).
        headers=[
            ("X-Frame-Options", "DENY"),
            ("Content-Security-Policy", "frame-ancestors 'none'"),
        ],
        workers=1,
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
    )


def migrate_config(store: Store) -> None:
    """Copy the files at a new HomeProxy version, then bring them up to this release's version,
    before anything reads them.

    A failure is logged and the dashboard still starts, so the admin can see it. A failed copy
    stops the migration, because a migration without a way back is worse. A file newer than this
    release is one such failure.
    """
    try:
        with store.lock() as held:
            copied = copy_for_upgrade(store, held, importlib.metadata.version("homeproxy"))
            if copied:
                logger.info("copied the config and secrets to %s", copied)
            migrated = migrate_files(store, held)
            prune_backups(store)
    except (OSError, StoreError) as error:
        logger.error("cannot copy or migrate the config files: %s", error)
        return
    if migrated:
        logger.info(
            "migrated %d config files; the originals are in %s",
            len(migrated),
            store.migration_backups_dir,
        )


def serve(environ: Mapping[str, str] | None = None) -> None:
    """Run the dashboard until it is stopped. Raises `ServerConfigError` on bad settings."""
    # The runtime builds a `ListenChange`, which uses this module, so import it late.
    from homeproxy.auth.runtime import AuthRuntime, data_root, set_runtime

    env = os.environ if environ is None else environ
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(name)s: %(message)s")
    store = Store(data_root())
    migrate_config(store)
    plan = plan_listen(store, env)
    certificate = store.root / "tls" / "dashboard.pem"
    if ensure_certificate(certificate, [plan.host]):
        logger.info("created a self-signed dashboard certificate at %s", certificate)
    print_fingerprint = fingerprint(certificate)
    logger.info("dashboard certificate SHA-256 fingerprint: %s", print_fingerprint)
    runtime = AuthRuntime.start(store)

    from homeproxy.main import app  # main imports this package, so import late

    set_runtime(app, runtime)
    # On the fallback address `network.lan_cidrs` may be as stale as the address, as after a new
    # router. A confirmed new address uses `runtime.source_policy` again.
    runtime.on_fallback = plan.reason == "fallback"
    policy = private_policy if runtime.on_fallback else runtime.source_policy
    shown_host = plan.host
    if shown_host == "0.0.0.0":  # noqa: S104 - compared, not bound
        shown_host = default_route_address() or "<this host's LAN address>"
    url = f"https://{shown_host}:{plan.port}"
    logger.info(
        "dashboard listening on %s:%s (%s), accepting %s",
        plan.host,
        plan.port,
        plan.reason,
        policy().label,
    )
    if runtime.setup_token is not None and plan.setup_mode:
        announce(runtime.setup_token, url, print_fingerprint)
    server = uvicorn.Server(build_config(plan, str(certificate), app, policy))
    # A confirmed change of `dashboard.listen` closes this server's listener.
    runtime.listen_change.serving = server
    server.run()
