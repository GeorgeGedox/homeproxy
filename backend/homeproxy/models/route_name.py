"""The name the server gives a route that is created without one."""

import re
from collections.abc import Callable

from homeproxy.models.route import HttpRoute, Route, TcpRoute

MAX_LENGTH = 63
"""The longest route name the `Name` pattern allows."""

_NOT_NAME = re.compile(r"[^a-z0-9]+")


def _source(route: Route) -> str:
    if isinstance(route, TcpRoute):
        return f"tcp-{route.listen_port}"
    path = route.path_prefix if isinstance(route, HttpRoute) else "/"
    return f"{route.hosts[0]}{path}"


def new_route_name(route: Route, taken: Callable[[str], bool]) -> str:
    """Return a free name from the first host plus the path, or `tcp-<port>`. `taken` says which
    names are in use.

    Every run of characters a name cannot hold becomes one hyphen, and the name is cut to fit, so
    `app.example.com` with `/api` gives `app-example-com-api`. A taken name gets `-2`, `-3`, ...
    """
    base = _NOT_NAME.sub("-", _source(route).lower()).strip("-")
    name, number = base[:MAX_LENGTH].rstrip("-"), 1
    while taken(name):
        number += 1
        suffix = f"-{number}"
        name = base[: MAX_LENGTH - len(suffix)].rstrip("-") + suffix
    return name
