"""Build `country.map`, the IP prefix to country code map that HAProxy reads with `map_ip`.

The full DB-IP Lite dataset, written as plain CIDR blocks, is about 1.4 million entries. HAProxy
holds each map entry in about 300 bytes, which alone breaks the 300 MB footprint. Two reductions
bring it to about 400 thousand entries:

- `map_ip` returns the longest matching prefix, so a wide prefix can carry the most common country
  of a block and longer prefixes only the exceptions. The ORTC algorithm (Draves et al., 1999)
  finds the smallest such map. It is exact.
- IPv6 is resolved to /32, the smallest block that registries hand to an ISP. Each /32 gets the
  country of its first address.

`MAX_ENTRIES` caps the result, so no dataset can push HAProxy past the footprint.

HAProxy needs only the countries that are blocked somewhere. `blocked_country_map` cuts a built
map down to them at each apply, so with nothing blocked HAProxy holds no map at all. The log
collector finds every country with `homeproxy.geoip.lookup` instead.
"""

import io
import ipaddress
import re
import socket
from bisect import bisect_left, bisect_right
from collections.abc import Collection
from dataclasses import dataclass, field

MAX_ENTRIES = 450_000
"""About 130 MB in HAProxy. The integration test proves the footprint with a map of this size."""

IPV6_PREFIX = 32
UNKNOWN = "ZZ"
"""DB-IP's code for an address with no country, such as a private range. It is logged as empty."""

_COUNTRY = re.compile(r"[A-Z]{2}")
_CHUNK_PREFIX = {32: 8, 128: 16}
"""The map is built one block of this size at a time, which bounds the memory a build needs."""

# A node is the countries it can take with the fewest entries, and its two halves unless the whole
# block has one country.
_Node = tuple[frozenset[str], "tuple[_Node, _Node] | None"]


class GeoipError(Exception):
    """GeoIP data could not be fetched or is not valid. The message is safe to show."""


@dataclass
class Ranges:
    """The countries of one address family, added in address order.

    Kept as contiguous segments that cover the family's whole space: segment `i` starts at
    `starts[i]` and runs to the next start. Gaps between added ranges get `UNKNOWN`.
    """

    bits: int
    starts: list[int] = field(default_factory=list)
    countries: list[str] = field(default_factory=list)
    _next: int = 0

    def add(self, start: int, end: int, country: str) -> None:
        """Add the inclusive range `start`..`end`. Raises `GeoipError` for a code that is not
        two capital letters, or for a range that starts before the end of the previous one."""
        if _COUNTRY.fullmatch(country) is None:
            raise GeoipError("a country code is not valid")
        if start < self._next or end < start or end >> self.bits:
            raise GeoipError("the ranges overlap or are not in address order")
        if start > self._next:
            self._append(self._next, UNKNOWN)
        self._append(start, country)
        self._next = end + 1

    def segments(self) -> tuple[list[int], list[str]]:
        """Return the starts and countries, with the space after the last range as `UNKNOWN`."""
        if self._next < 1 << self.bits:
            self._append(self._next, UNKNOWN)
            self._next = 1 << self.bits
        return self.starts, self.countries

    def _append(self, start: int, country: str) -> None:
        if self.countries and self.countries[-1] == country:
            return
        self.starts.append(start)
        self.countries.append(country)


def build_country_map(ipv4: Ranges, ipv6: Ranges) -> str:
    """Return the text of `country.map`. Raises `GeoipError` above `MAX_ENTRIES` entries."""
    lines = _entries(ipv4, 32) + _entries(ipv6, IPV6_PREFIX)
    if len(lines) > MAX_ENTRIES:
        raise GeoipError(
            f"the country map has {len(lines)} entries, more than the {MAX_ENTRIES} that fit in "
            "HomeProxy's memory limit"
        )
    return "".join(lines)


def blocked_country_map(text: str, blocked: Collection[str]) -> str:
    """Return the part of a built map that finds the countries in `blocked`.

    Every address keeps its country when that country is blocked and gets none otherwise. An
    entry of another country stays, as `UNKNOWN`, only where it is an exception inside a blocked
    country's prefix. This relies on the builder's order: each prefix before the prefixes inside it.
    """
    if not blocked:
        return ""
    # Per family, the end and the country of each kept prefix that holds the current one.
    outer: dict[bool, list[tuple[int, str]]] = {False: [], True: []}
    lines: list[str] = []
    for line in io.StringIO(text):
        network, _, country = line.rstrip("\n").partition(" ")
        address, _, length = network.partition("/")
        ipv6 = ":" in address
        packed = socket.inet_pton(socket.AF_INET6 if ipv6 else socket.AF_INET, address)
        start = int.from_bytes(packed)
        stack = outer[ipv6]
        while stack and stack[-1][0] <= start:
            stack.pop()
        inherited = stack[-1][1] if stack else UNKNOWN
        own = country if country in blocked else UNKNOWN
        if own != inherited:
            lines.append(f"{network} {own}\n")
        stack.append((start + (1 << (len(packed) * 8 - int(length))), own))
    return "".join(lines)


def _entries(ranges: Ranges, leaf_prefix: int) -> list[str]:
    starts, countries = ranges.segments()
    bits, chunk_prefix = ranges.bits, _CHUNK_PREFIX[ranges.bits]
    network = ipaddress.IPv4Network if bits == 32 else ipaddress.IPv6Network
    singles = {country: frozenset((country,)) for country in set(countries)}
    lines: list[str] = []
    for chunk in range(1 << chunk_prefix):
        base = chunk << (bits - chunk_prefix)
        tree = _node(starts, countries, singles, base, bits - chunk_prefix, bits - leaf_prefix)
        found: list[tuple[int, int, str]] = []
        _emit(tree, base, bits - chunk_prefix, UNKNOWN, found)
        lines += (f"{network((start, bits - size))} {country}\n" for start, size, country in found)
    return lines


def _node(
    starts: list[int],
    countries: list[str],
    singles: dict[str, frozenset[str]],
    base: int,
    size: int,
    leaf_size: int,
) -> _Node:
    """Build the tree of the block of `2**size` addresses at `base`, keeping ORTC's first pass:
    the countries common to both halves, or all of theirs when they share none."""
    index = bisect_right(starts, base) - 1
    one_country = bisect_left(starts, base + (1 << size), index + 1) == index + 1
    if one_country or size <= leaf_size:
        return singles[countries[index]], None
    left = _node(starts, countries, singles, base, size - 1, leaf_size)
    right = _node(starts, countries, singles, base + (1 << (size - 1)), size - 1, leaf_size)
    common = left[0] & right[0]
    return common or left[0] | right[0], (left, right)


def _emit(
    node: _Node, base: int, size: int, inherited: str, found: list[tuple[int, int, str]]
) -> None:
    """Write an entry only where the country a block inherits is not one it can take."""
    choices, halves = node
    country = inherited
    if inherited not in choices:
        country = min(choices)
        found.append((base, size, country))
    if halves is not None:
        _emit(halves[0], base, size - 1, country, found)
        _emit(halves[1], base + (1 << (size - 1)), size - 1, country, found)
