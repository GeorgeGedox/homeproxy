"""The log collector's country lookup: binary search over the arrays the refresh writes."""

import ipaddress
import logging
from collections.abc import Callable
from pathlib import Path

import pytest

from homeproxy.geoip import lookup
from homeproxy.geoip.country_map import UNKNOWN, Ranges
from homeproxy.geoip.data import DATA_DIR
from homeproxy.geoip.lookup import LOOKUP_FILE, CountryLookup, write_lookup


def ranges_of(bits: int, *rows: tuple[str, str, str]) -> Ranges:
    ranges = Ranges(bits)
    for start, end, country in rows:
        ranges.add(int(ipaddress.ip_address(start)), int(ipaddress.ip_address(end)), country)
    return ranges


def saved(root: Path, ipv4: Ranges, ipv6: Ranges) -> CountryLookup:
    (root / DATA_DIR).mkdir(exist_ok=True)
    write_lookup(root / DATA_DIR, ipv4, ipv6)
    return CountryLookup(root)


FIRST_IPV4 = str(ipaddress.IPv4Address(0))
IPV4 = (
    (FIRST_IPV4, "0.255.255.255", "AU"),
    ("1.0.0.0", "1.0.0.255", "CN"),
    # 1.0.1.0 to 1.0.1.255 is a gap.
    ("1.0.2.0", "1.0.2.0", "JP"),
    ("1.0.2.1", "1.0.3.255", UNKNOWN),
    ("255.255.255.0", "255.255.255.255", "US"),
)
IPV6 = (
    ("::", "::ffff:ffff:ffff:ffff", "DE"),
    ("2001:db8::", "2001:db8:0:1:ffff:ffff:ffff:ffff", "FR"),
    ("2001:db8:0:2::", "2001:db8:0:2:ffff:ffff:ffff:ffff", "NL"),
    # One /64 split in two.
    ("2001:db8:0:4::", "2001:db8:0:4::ff", "IT"),
    ("2001:db8:0:4::100", "2001:db8:0:4:ffff:ffff:ffff:ffff", "ES"),
    ("ffff:ffff:ffff:ffff::", "ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff", "SE"),
)


@pytest.mark.parametrize(
    ("address", "country"),
    [
        pytest.param(FIRST_IPV4, "AU", id="first-ipv4-address"),
        pytest.param("1.0.0.0", "CN", id="start-of-range"),
        pytest.param("1.0.0.255", "CN", id="end-of-range"),
        pytest.param("1.0.1.0", None, id="start-of-gap"),
        pytest.param("1.0.2.0", "JP", id="one-address-range"),
        pytest.param("1.0.2.1", None, id="range-without-country"),
        pytest.param("1.0.4.0", None, id="after-the-ranges"),
        pytest.param("255.255.255.255", "US", id="last-ipv4-address"),
        pytest.param("::", "DE", id="first-ipv6-address"),
        pytest.param("0:0:0:1::", None, id="first-ipv6-gap"),
        pytest.param("2001:db8::", "FR", id="start-of-ipv6-range"),
        pytest.param("2001:db8:0:1:ffff:ffff:ffff:ffff", "FR", id="end-of-ipv6-range"),
        pytest.param("2001:db8:0:2::1", "NL", id="next-slash-64"),
        pytest.param("2001:db8:0:3::", None, id="ipv6-gap"),
        pytest.param("2001:db8:0:4::1", "ES", id="last-segment-wins-in-a-split-slash-64"),
        pytest.param("ffff:ffff:ffff:fffe:ffff::", None, id="before-last-ipv6-range"),
        pytest.param("ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff", "SE", id="last-ipv6-address"),
        pytest.param("1.0.0", None, id="not-an-address"),
    ],
)
def test_every_address_gets_the_country_of_its_range(
    tmp_path: Path, address: str, country: str | None
) -> None:
    countries = saved(tmp_path, ranges_of(32, *IPV4), ranges_of(128, *IPV6))

    assert countries.country(address) == country


def test_no_file_means_no_country(tmp_path: Path) -> None:
    assert CountryLookup(tmp_path).country("1.0.0.1") is None


def test_a_refresh_replaces_the_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lookup, "RECHECK_SECONDS", 0.0)
    countries = saved(tmp_path, ranges_of(32, ("1.0.0.0", "1.0.0.255", "CN")), Ranges(128))
    assert countries.country("1.0.0.1") == "CN"

    write_lookup(tmp_path / DATA_DIR, ranges_of(32, ("1.0.0.0", "1.0.0.255", "AU")), Ranges(128))

    assert countries.country("1.0.0.1") == "AU"


@pytest.mark.parametrize(
    "damage",
    [
        pytest.param(lambda data: data[:-1], id="truncated"),
        pytest.param(lambda data: b"X" + data[1:], id="wrong-magic"),
        pytest.param(lambda data: data[:-2] + b'"\n', id="code-not-capital-letters"),
        pytest.param(lambda data: b"", id="empty"),
    ],
)
def test_a_damaged_file_gives_no_country(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, damage: Callable[[bytes], bytes]
) -> None:
    countries = saved(tmp_path, ranges_of(32, ("1.0.0.0", "1.0.0.255", "CN")), Ranges(128))
    path = tmp_path / DATA_DIR / LOOKUP_FILE
    path.write_bytes(damage(path.read_bytes()))

    with caplog.at_level(logging.WARNING):
        assert countries.country("1.0.0.1") is None

    assert "is not valid" in caplog.text
