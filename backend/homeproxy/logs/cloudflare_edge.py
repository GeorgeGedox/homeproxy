"""Find routes that Cloudflare proxies although HomeProxy does not mark them proxied.

HAProxy replaces the source of a request from Cloudflare with `CF-Connecting-IP` only for a route
in `proxied-only.map`. For any other route the access log holds the address of the Cloudflare edge,
so the logs, GeoIP and CrowdSec do not see the visitor. `CloudflareEdge` is a log listener that
notes when a line from an edge address arrives, and tells which of those routes are not marked.
It reads only logged client addresses, so it works with the Cloudflare integration off.
"""

import ipaddress
import logging
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Final

from homeproxy.generator import BACKEND_PREFIX
from homeproxy.logs.parser import AccessLog

logger = logging.getLogger(__name__)

# shortcut: sightings are in memory and a restart of the dashboard clears them, persist them if
# users report a lost warning.
WARNING_SECONDS: Final = 24 * 3600
"""How long a route warns after the last request from a Cloudflare edge address."""

RELOAD_SECONDS: Final = 5.0
"""How often the listener checks whether `cloudflare.lst` changed."""

_PROXIED_ONLY_FILE = "proxied-only.map"

type _Network = ipaddress.IPv4Network | ipaddress.IPv6Network


def _parse_ranges(text: str) -> list[_Network]:
    networks: list[_Network] = []
    for line in text.splitlines():
        try:
            networks.append(ipaddress.ip_network(line.strip(), strict=False))
        except ValueError:
            continue
    return networks


class CloudflareEdge:
    """Remember when each route last got a request from an address in `cloudflare.lst`."""

    def __init__(self, ranges_file: Path, clock: Callable[[], float] = time.monotonic) -> None:
        self._file = ranges_file
        self._clock = clock
        self._networks: list[_Network] = []
        self._version: tuple[int, int] | None = None
        self._checked: float | None = None
        self._seen: dict[str, float] = {}

    def __call__(self, line: AccessLog) -> None:
        """Note the route of a line from a Cloudflare edge address."""
        if not line.route:
            return
        now = self._clock()
        self._reload(now)
        try:
            address = ipaddress.ip_address(line.client_ip)
        except ValueError:
            return
        if any(address in network for network in self._networks):
            self._seen[line.route] = now

    def _reload(self, now: float) -> None:
        """Read the list again when it changed. The live release changes on each apply."""
        if self._checked is not None and now - self._checked < RELOAD_SECONDS:
            return
        self._checked = now
        try:
            stat = self._file.stat()
            version = (stat.st_ino, stat.st_mtime_ns)
            if version != self._version:
                self._networks = _parse_ranges(self._file.read_text())
                self._version = version
        except OSError as error:
            logger.warning("cannot read the cloudflare range list: %s", type(error).__name__)
            self._networks, self._version = [], None

    def unmarked(self, release: Path, live: Iterable[str]) -> list[str]:
        """Return the sorted `live` routes that got a request from a Cloudflare edge address in the
        last `WARNING_SECONDS` and are not in the `proxied-only.map` of `release`."""
        cutoff = self._clock() - WARNING_SECONDS
        recent = {route for route, seen in self._seen.items() if seen >= cutoff} & set(live)
        if not recent:
            return []
        try:
            text = (release / _PROXIED_ONLY_FILE).read_text()
        except OSError:
            return []
        marked = {
            line.split()[0].removeprefix(BACKEND_PREFIX)
            for line in text.splitlines()
            if line.strip()
        }
        return sorted(recent - marked)
