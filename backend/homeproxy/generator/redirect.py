"""`redirect` routes: HAProxy answers every request on their hosts with a redirect.

A redirect host is in `hosts.map` like any other host, so the LAN-only, GeoIP and CrowdSec checks
apply to it by backend name, although no such backend exists. `redirect.map` is keyed by that
backend name, so it cannot disagree with `hosts.map` on a host. Its value is
`<code>|<keep path: 0 or 1>|<target>`. The frontend reads the three fields with `field()`.
"""

from collections.abc import Iterable, Iterator

from homeproxy.generator.escape import number, redirect_url, token
from homeproxy.generator.route_types import BACKEND_PREFIX
from homeproxy.models import RedirectRoute

REDIRECT_MAP = "redirect.map"

REDIRECT_CODES = (301, 302, 307, 308)
"""HAProxy needs the status code in the config, so the frontend has one rule for each."""


def redirect_entries(routes: Iterable[RedirectRoute]) -> Iterator[str]:
    """Yield a `<backend> <code>|<keep path>|<target>` line of `redirect.map` for each route."""
    for route in routes:
        redirect = route.redirect
        # The request path starts with `/`, so a trailing one here would double it.
        target = redirect.to.rstrip("/") if redirect.keep_path else redirect.to
        keep = number(int(redirect.keep_path))
        yield (
            f"{token(BACKEND_PREFIX + route.name)} {number(redirect.code)}|{keep}|"
            f"{redirect_url(target)}"
        )
