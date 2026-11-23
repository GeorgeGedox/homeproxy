"""The country of every logged request, from the full GeoIP data in sorted arrays.

HAProxy's `country.map` holds only the countries blocked somewhere, so the log collector finds
each client's country itself. The refresh writes `LOOKUP_FILE` next to the map: for each address
family, the start of each segment of the address space in order, and the segment's country code.
`CountryLookup` loads it into `array`s and finds an address with one binary search, which takes
about a microsecond. The full DB-IP data takes about 6 MB.

IPv6 starts are kept to /64, which no ISP splits between countries. Where data does split a /64,
its addresses take the country of its last segment.

The arrays are in the machine's byte order: the refresh writes them on the machine that reads them.
"""

import logging
import re
import socket
import struct
import time
from array import array
from bisect import bisect_right
from dataclasses import dataclass
from pathlib import Path

from homeproxy.geoip.country_map import UNKNOWN, Ranges
from homeproxy.geoip.data import DATA_DIR
from homeproxy.util.atomic import atomic_write

logger = logging.getLogger(__name__)

LOOKUP_FILE = "country-lookup.bin"
RECHECK_SECONDS = 10.0
"""How often `CountryLookup` looks for a file that a refresh replaced."""

_MAGIC = b"HPGEO\x00\x00\x01"
_HEADER = struct.Struct("=8sQQ")
_CODES = re.compile(rb"(?:[A-Z]{2})*")


@dataclass(frozen=True)
class _Family:
    starts: array[int]
    codes: bytes

    def country(self, key: int) -> str | None:
        index = bisect_right(self.starts, key) - 1
        code = self.codes[2 * index : 2 * index + 2].decode("ascii")
        return None if code == UNKNOWN else code


def write_lookup(directory: Path, ipv4: Ranges, ipv6: Ranges) -> None:
    """Write the lookup file of both families into `directory`, replacing the old one."""
    starts4, countries4 = ipv4.segments()
    starts6, countries6 = ipv6.segments()
    data = b"".join(
        (
            _HEADER.pack(_MAGIC, len(starts4), len(starts6)),
            array("I", starts4).tobytes(),
            array("Q", [start >> 64 for start in starts6]).tobytes(),
            "".join(countries4 + countries6).encode("ascii"),
        )
    )
    atomic_write(directory / LOOKUP_FILE, data, mode=0o644)


def _load(path: Path) -> tuple[_Family, _Family] | None:
    """Read a lookup file, or return None with a warning when it is not one the refresh wrote."""
    data = path.read_bytes()
    try:
        magic, count4, count6 = _HEADER.unpack_from(data)
    except struct.error:
        magic = count4 = count6 = 0
    starts4, starts6 = array("I"), array("Q")
    starts6_at = _HEADER.size + count4 * starts4.itemsize
    codes_at = starts6_at + count6 * starts6.itemsize
    codes = data[codes_at:]
    if magic == _MAGIC and len(codes) == 2 * (count4 + count6):
        starts4.frombytes(data[_HEADER.size : starts6_at])
        starts6.frombytes(data[starts6_at:codes_at])
    # Each family's first segment starts at its first address, so every address has one.
    if not list(starts4[:1]) == [0] == list(starts6[:1]) or _CODES.fullmatch(codes) is None:
        logger.warning("%s is not valid; logs show only the countries HAProxy blocks", path)
        return None
    return _Family(starts4, codes[: 2 * count4]), _Family(starts6, codes[2 * count4 :])


class CountryLookup:
    """The country of an IP address, from the lookup file in a data directory.

    It loads the file on first use, and again when a refresh has replaced it. Without a file,
    every address has no country.
    """

    def __init__(self, data_dir: Path) -> None:
        """Read `LOOKUP_FILE` from the GeoIP directory of `data_dir` when first needed."""
        self.path = data_dir / DATA_DIR / LOOKUP_FILE
        self._families: tuple[_Family, _Family] | None = None
        self._stamp: tuple[int, int] | None = None
        self._checked_at = float("-inf")

    def country(self, address: str) -> str | None:
        """Return the country code of an IPv4 or IPv6 address, or None when it has none."""
        families = self._current()
        if families is None:
            return None
        try:
            if ":" in address:
                return families[1].country(
                    int.from_bytes(socket.inet_pton(socket.AF_INET6, address)) >> 64
                )
            return families[0].country(int.from_bytes(socket.inet_pton(socket.AF_INET, address)))
        except OSError:
            return None

    def _current(self) -> tuple[_Family, _Family] | None:
        now = time.monotonic()
        if now - self._checked_at < RECHECK_SECONDS:
            return self._families
        self._checked_at = now
        try:
            status = self.path.stat()
            stamp = (status.st_ino, status.st_mtime_ns)
            if stamp != self._stamp:
                self._families, self._stamp = _load(self.path), stamp
        except FileNotFoundError:
            self._families, self._stamp = None, None
        except OSError as error:
            logger.warning("cannot read %s: %s", self.path, error)
        return self._families
