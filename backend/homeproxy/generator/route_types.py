"""Route names, and the routes HAProxy forwards without reading: `passthrough` and `tcp`.

A `passthrough` route is picked by the SNI name of the TLS hello on the HTTPS port. When one
exists, a TCP frontend owns that port. It hands passthrough connections to their own frontend and
every other connection to the HTTPS frontend, each over an internal socket, with the PROXY protocol
so the client address survives. A `tcp` route has its own port. Both kinds get `mode tcp` backends.
"""

from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from homeproxy.generator.escape import GeneratorError, token
from homeproxy.models.route import HttpRoute, PassthroughRoute, TcpRoute

BACKEND_PREFIX = "route-"
"""Route names may be HAProxy keywords such as `global`, or end in `-`, so backends get a prefix."""

HTTPS_SOCKET = "/run/haproxy/https.sock"
"""Where the HTTPS frontend listens while the SNI layer owns the HTTPS port."""

PASSTHROUGH_SOCKET = "/run/haproxy/passthrough.sock"
"""Where the SNI layer hands passthrough connections on, to be checked and logged."""

SNI_MAP = "sni.map"
"""Passthrough hosts and their backends. It exists only while a passthrough route does."""


class RouteError(GeneratorError):
    """A route that passes its model cannot be rendered. The message holds no header value."""

    def __init__(self, route: str, field: str, reason: str) -> None:
        """Name the route and field that failed, with a reason safe to show to the admin."""
        super().__init__(f"route {route!r}: {field}: {reason}")


@dataclass(frozen=True)
class StreamBackend:
    """A `mode tcp` backend. `listen_port` is set for a `tcp` route. Servers are named after their
    address, as in HTTP backends, so the server state file never keeps an old address."""

    name: str
    listen_port: int | None
    servers: tuple[str, ...]
    sticky: bool


def upstream_addresses(route: HttpRoute | PassthroughRoute | TcpRoute) -> list[str]:
    """Return the route's upstream addresses. HAProxy refuses two servers with one name."""
    addresses = [upstream.address for upstream in route.upstreams]
    if len(set(addresses)) != len(addresses):
        raise RouteError(route.name, "upstreams", "each upstream address may appear only once")
    return addresses


def stream_backend(route: PassthroughRoute | TcpRoute) -> StreamBackend:
    """Return the backend of a `passthrough` or `tcp` route."""
    port = route.listen_port if isinstance(route, TcpRoute) else None
    addresses = tuple(upstream_addresses(route))
    return StreamBackend(BACKEND_PREFIX + route.name, port, addresses, route.sticky)


def sni_entries(routes: Iterable[PassthroughRoute]) -> Iterator[str]:
    """Yield a `<host> <backend>` line of `sni.map` for each host of each passthrough route."""
    for route in routes:
        for host in route.hosts:
            yield f"{token(host)} {token(BACKEND_PREFIX + route.name)}"
