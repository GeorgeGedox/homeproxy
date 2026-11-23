"""Cloudflare's edge ranges: the only sources HAProxy lets name the client in `CF-Connecting-IP`.

The refresh job saves Cloudflare's published lists to `state/cloudflare.lst` in the data directory,
and each apply renders that file into the release as `cloudflare.lst`. Before the first refresh,
the built-in copy below is used.
"""

import ipaddress
import logging
from collections.abc import Iterable
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from homeproxy.generator.escape import token
from homeproxy.models.fields import IpOrCidr

logger = logging.getLogger(__name__)

CLOUDFLARE_LIST = "cloudflare.lst"
"""The release file, and the runtime name of the ACL that `-f cloudflare.lst` loads."""

CACHE_FILE = Path("state") / CLOUDFLARE_LIST
"""Where the refresh job keeps the last lists it fetched, relative to the data directory."""

DEFAULT_RANGES = (
    # https://www.cloudflare.com/ips-v4 and https://www.cloudflare.com/ips-v6, November 2026.
    "173.245.48.0/20",
    "103.21.244.0/22",
    "103.22.200.0/22",
    "103.31.4.0/22",
    "141.101.64.0/18",
    "108.162.192.0/18",
    "190.93.240.0/20",
    "188.114.96.0/20",
    "197.234.240.0/22",
    "198.41.128.0/17",
    "162.158.0.0/15",
    "104.16.0.0/13",
    "104.24.0.0/14",
    "172.64.0.0/13",
    "131.0.72.0/22",
    "2400:cb00::/32",
    "2606:4700::/32",
    "2803:f800::/32",
    "2405:b500::/32",
    "2405:8100::/32",
    "2a06:98c0::/29",
    "2c0f:f248::/32",
)

_RANGE: TypeAdapter[str] = TypeAdapter(IpOrCidr)
_MIN_PREFIX = {4: 8, 6: 16}
"""Cloudflare's largest ranges are /12 and /29. A shorter prefix cannot be one of them."""


class RangesError(Exception):
    """A Cloudflare range list could not be read or is not valid. The message is safe to show."""


def parse_ranges(text: str) -> tuple[str, ...]:
    """Return the ranges of a list with one address or range per line, ignoring blank lines.

    Every line must be a range as `lan_cidrs` accepts it, so a comment or a second word fails the
    whole list. So does a range that cannot be Cloudflare's edge: one that is not public, or one
    so wide that a few of them cover every client. An empty list fails too, as it trusts nobody.
    """
    ranges = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = _RANGE.validate_python(line.strip())
        except ValidationError:
            raise RangesError(f"line {number} is not an IP address or range") from None
        network = ipaddress.ip_network(value)
        if not network.is_global or network.prefixlen < _MIN_PREFIX[network.version]:
            raise RangesError(f"line {number} cannot be a Cloudflare range")
        ranges.append(value)
    if not ranges:
        raise RangesError("the list is empty")
    return tuple(ranges)


def ranges_file(ranges: Iterable[str]) -> str:
    """Return the text of `cloudflare.lst`, sorted so that the same ranges give the same bytes."""
    return "".join(f"{token(item)}\n" for item in sorted(ranges))


def load_ranges(data_dir: Path) -> tuple[str, ...]:
    """Return the ranges the refresh job saved in `data_dir`, or the built-in ones.

    A saved file that is not valid is logged and replaced by the built-in ranges, so an apply never
    fails over a list the admin cannot fix.
    """
    path = data_dir / CACHE_FILE
    try:
        return parse_ranges(path.read_text(encoding="ascii"))
    except FileNotFoundError:
        return DEFAULT_RANGES
    except (UnicodeDecodeError, RangesError) as exc:
        logger.warning("%s is not valid, using the built-in Cloudflare ranges: %s", path, exc)
        return DEFAULT_RANGES
