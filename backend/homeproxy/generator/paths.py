"""Path routes: `http` routes that share a host and differ by `path_prefix`.

A host where any enabled route has a prefix other than `/` is a path host. Its routes are in
`paths.map` instead of `hosts.map`, keyed `<host><prefix>/`. The frontend looks up `<host><path>/`
with `map_beg`, so the `/` on both ends a prefix on a segment boundary: `/api/` begins `/api/` and
`/api/x/`, never `/apiv2/`. HAProxy keeps `map_beg` patterns in a prefix tree, so the longest
prefix wins whatever the order of the lines. A path host without a `/` route gets `<host>/` on
`be_not_found`, so a path that no route serves answers 404 as a known host.
"""

from collections.abc import Iterable, Iterator

from homeproxy.generator.escape import path_prefix, token
from homeproxy.generator.route_types import BACKEND_PREFIX
from homeproxy.models import HttpRoute

PATHS_MAP = "paths.map"
NOT_FOUND_BACKEND = "be_not_found"
"""The 404 backend of a host that a route serves, on a path or a port that no route answers."""


def path_hosts(routes: Iterable[HttpRoute]) -> set[str]:
    """Return the hosts that a route serves on a prefix other than `/`."""
    return {host for route in routes if route.path_prefix != "/" for host in route.hosts}


def path_entries(routes: Iterable[HttpRoute], hosts: set[str]) -> Iterator[str]:
    """Yield a `<host><prefix>/ <backend>` line of `paths.map` for each route on a path host, and a
    `<host>/` line on `NOT_FOUND_BACKEND` for a path host with no `/` route."""
    rooted: set[str] = set()
    for route in routes:
        prefix = path_prefix(route.path_prefix).rstrip("/")
        for host in hosts.intersection(route.hosts):
            yield f"{token(f'{host}{prefix}/')} {token(BACKEND_PREFIX + route.name)}"
            if not prefix:
                rooted.add(host)
    for host in sorted(hosts - rooted):
        yield f"{token(f'{host}/')} {NOT_FOUND_BACKEND}"
