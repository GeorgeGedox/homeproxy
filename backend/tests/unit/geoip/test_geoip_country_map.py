"""Building `country.map`: exact lookups with fewer entries, IPv6 per /32, the size cap, and the
cut to blocked countries."""

import ipaddress
import random

import pytest

from homeproxy.geoip import country_map
from homeproxy.geoip.country_map import (
    UNKNOWN,
    GeoipError,
    Ranges,
    blocked_country_map,
    build_country_map,
)


def lookup(text: str, address: str) -> str:
    """Return what HAProxy's `map_ip` finds: the country of the longest matching prefix."""
    ip = ipaddress.ip_address(address)
    best: tuple[int, str] = (-1, UNKNOWN)
    for line in text.splitlines():
        prefix, country = line.split(" ")
        network = ipaddress.ip_network(prefix)
        if ip in network and network.prefixlen > best[0]:
            best = (network.prefixlen, country)
    return best[1]


def ranges_of(bits: int, *rows: tuple[str, str, str]) -> Ranges:
    ranges = Ranges(bits)
    for start, end, country in rows:
        ranges.add(int(ipaddress.ip_address(start)), int(ipaddress.ip_address(end)), country)
    return ranges


BASE = int(ipaddress.IPv4Address("10.20.0.0"))


def random_ranges() -> tuple[Ranges, dict[int, str], int]:
    """Random IPv4 ranges with gaps and ZZ holes from `BASE`, the country of each address, and
    the number of CIDR blocks they take."""
    rng = random.Random(4)  # noqa: S311 - test data, not a secret
    expected: dict[int, str] = {}
    ipv4 = Ranges(32)
    blocks = 0
    address = BASE
    while address < BASE + 2048:
        size = rng.choice([1, 3, 8, 16, 61, 128, 200])
        country = rng.choice(["DE", "FR", "US", UNKNOWN])
        if rng.random() < 0.8:  # the rest is a gap
            ipv4.add(address, address + size - 1, country)
            start, end = ipaddress.IPv4Address(address), ipaddress.IPv4Address(address + size - 1)
            blocks += len(list(ipaddress.summarize_address_range(start, end)))
            expected.update(dict.fromkeys(range(address, address + size), country))
        address += size
    return ipv4, expected, blocks


def test_map_gives_every_address_its_country_with_fewer_entries_than_cidr_blocks() -> None:
    """Checked address by address around the random ranges."""
    ipv4, expected, blocks = random_ranges()

    text = build_country_map(ipv4, Ranges(128))

    assert len(text.splitlines()) < blocks * 0.8
    for address in range(BASE - 16, max(expected) + 16):
        expect = expected.get(address, UNKNOWN)
        assert lookup(text, str(ipaddress.IPv4Address(address))) == expect, address


@pytest.mark.parametrize(
    "blocked", [{"DE"}, {"FR", "US"}, {"DE", "FR", "US"}], ids=["DE", "FR-US", "all"]
)
def test_blocked_map_finds_only_blocked_countries(blocked: set[str]) -> None:
    """Every address keeps a blocked country and loses any other, IPv4 and IPv6 alike."""
    ipv4, expected, _ = random_ranges()
    ipv6 = ranges_of(
        128,
        ("2001:db8::", "2001:dbf:ffff:ffff:ffff:ffff:ffff:ffff", "FR"),
        ("2001:dc0::", "2001:dc0:ffff:ffff:ffff:ffff:ffff:ffff", "DE"),
        ("2001:dc1::", "2001:dc7:ffff:ffff:ffff:ffff:ffff:ffff", "FR"),
    )
    text = build_country_map(ipv4, ipv6)

    cut = blocked_country_map(text, blocked)

    for address in range(BASE - 16, max(expected) + 16):
        country = expected.get(address, UNKNOWN)
        expect = country if country in blocked else UNKNOWN
        assert lookup(cut, str(ipaddress.IPv4Address(address))) == expect, address
    for address6, country in [("2001:db8::1", "FR"), ("2001:dc0::1", "DE"), ("2001:dc7::", "FR")]:
        assert lookup(cut, address6) == (country if country in blocked else UNKNOWN)


def test_nothing_blocked_gives_an_empty_map() -> None:
    text = build_country_map(ranges_of(32, ("1.0.0.0", "1.0.0.255", "AU")), Ranges(128))

    assert blocked_country_map(text, set()) == ""


def test_ipv6_takes_the_country_of_the_first_address_of_each_slash_32() -> None:
    ipv6 = ranges_of(
        128,
        ("2001:db8::", "2001:db8:0:ffff:ffff:ffff:ffff:ffff", "DE"),
        ("2001:db8:1::", "2001:db9:ffff:ffff:ffff:ffff:ffff:ffff", "FR"),
    )

    text = build_country_map(Ranges(32), ipv6)

    assert lookup(text, "2001:db8::1") == "DE"
    assert lookup(text, "2001:db8:1::1") == "DE"
    assert lookup(text, "2001:db9::1") == "FR"
    assert lookup(text, "2001:dba::1") == UNKNOWN


def test_private_hole_inside_a_country_stays_unknown() -> None:
    ipv4 = ranges_of(
        32,
        ("1.0.0.0", "1.0.1.255", "CN"),
        ("1.0.2.0", "1.0.2.255", UNKNOWN),
        ("1.0.3.0", "1.0.3.255", "CN"),
    )

    text = build_country_map(ipv4, Ranges(128))

    assert "1.0.2.0/24 ZZ\n" in text
    assert [lookup(text, f"1.0.{third}.1") for third in range(5)] == ["CN", "CN", "ZZ", "CN", "ZZ"]


@pytest.mark.parametrize(
    ("first", "second", "error"),
    [
        (("1.0.0.0", "1.0.0.255", "DE"), ("1.0.0.128", "1.0.1.255", "FR"), "overlap"),
        (("1.0.1.0", "1.0.1.255", "DE"), ("1.0.0.0", "1.0.0.255", "FR"), "order"),
        (("1.0.0.0", "1.0.0.255", "DE"), ("1.0.2.0", "1.0.1.0", "FR"), "order"),
        (("1.0.0.0", "1.0.0.255", "DE"), ("1.0.1.0", "1.0.1.255", "de"), "country code"),
        (("1.0.0.0", "1.0.0.255", "DE"), ("1.0.1.0", "1.0.1.255", "D\n"), "country code"),
    ],
    ids=["overlap", "out-of-order", "end-before-start", "lower-case", "newline"],
)
def test_ranges_refuse_data_that_is_not_valid(
    first: tuple[str, str, str], second: tuple[str, str, str], error: str
) -> None:
    with pytest.raises(GeoipError, match=error):
        ranges_of(32, first, second)


def test_range_past_the_end_of_the_address_space_is_refused() -> None:
    with pytest.raises(GeoipError):
        Ranges(32).add(0, 1 << 32, "DE")


def test_map_over_the_cap_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(country_map, "MAX_ENTRIES", 1)
    ipv4 = ranges_of(32, ("1.0.0.0", "1.0.0.255", "AU"), ("2.0.0.0", "2.0.0.255", "FR"))

    with pytest.raises(GeoipError, match="2 entries, more than the 1"):
        build_country_map(ipv4, Ranges(128))
