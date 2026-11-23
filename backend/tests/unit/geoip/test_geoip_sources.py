"""Downloading and reading DB-IP Lite and MaxMind GeoLite2 data, with a local stand-in server."""

import io
import zipfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from homeproxy.geoip import sources
from homeproxy.geoip.country_map import GeoipError, build_country_map
from homeproxy.geoip.sources import Provider, fetch_ranges
from tests.unit.geoip.conftest import GeoipServer

DBIP_ROWS = (
    "1.0.0.0,1.0.0.255,AU\n"
    "1.0.1.0,1.0.3.255,CN\n"
    "2001:200::,2001:200:ffff:ffff:ffff:ffff:ffff:ffff,JP\n"
)
DBIP_MAP = "1.0.0.0/22 CN\n1.0.0.0/24 AU\n2001:200::/32 JP\n"
LICENSE_KEY = "license-key-value"

MAXMIND_LOCATIONS = """\
geoname_id,locale_code,continent_code,continent_name,country_iso_code,country_name,is_in_european_union
2921044,en,EU,Europe,DE,Germany,1
6255148,en,EU,Europe,,,0
3017382,en,EU,Europe,FR,France,1
"""
BLOCKS_HEADER = (
    "network,geoname_id,registered_country_geoname_id,represented_country_geoname_id,"
    "is_anonymous_proxy,is_satellite_provider,is_anycast\n"
)
MAXMIND_IPV4 = BLOCKS_HEADER + (
    "2.0.0.0/24,2921044,2921044,,0,0,\n"
    # No country where it is: the registered country counts.
    "2.0.1.0/24,,3017382,,0,0,\n"
    # A continent, not a country.
    "2.0.2.0/24,6255148,6255148,,0,0,\n"
)
MAXMIND_IPV6 = BLOCKS_HEADER + "2a01::/32,3017382,3017382,,0,0,\n"


def maxmind_archive(blocks_ipv4: str = MAXMIND_IPV4) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        folder = "GeoLite2-Country-CSV_20261104"
        archive.writestr(f"{folder}/GeoLite2-Country-Locations-en.csv", MAXMIND_LOCATIONS)
        archive.writestr(f"{folder}/GeoLite2-Country-Blocks-IPv4.csv", blocks_ipv4)
        archive.writestr(f"{folder}/GeoLite2-Country-Blocks-IPv6.csv", MAXMIND_IPV6)
    return buffer.getvalue()


def fetch_country_map(provider: Provider, license_key: str, directory: Path) -> str:
    return build_country_map(*fetch_ranges(provider, license_key, directory))


def last_month() -> date:
    today = datetime.now(UTC).date()
    return date(today.year, today.month, 1) - timedelta(days=1)


def test_dbip_data_becomes_the_country_map(geoip_server: GeoipServer, tmp_path: Path) -> None:
    geoip_server.dbip(DBIP_ROWS)

    assert fetch_country_map("dbip-lite", "", tmp_path) == DBIP_MAP
    # The download is a temporary file.
    assert list(tmp_path.iterdir()) == []


def test_dbip_falls_back_to_last_months_file(geoip_server: GeoipServer, tmp_path: Path) -> None:
    """DB-IP publishes each month's file a little after the month starts."""
    geoip_server.dbip(DBIP_ROWS, last_month())

    assert fetch_country_map("dbip-lite", "", tmp_path) == DBIP_MAP
    assert len(geoip_server.requests) == 2


@pytest.mark.parametrize(
    ("row", "error"),
    [
        ("1.0.1.0,not-an-address,CN", "line 2: not a valid row"),
        ("1.0.1.0,1.0.1.255", "line 2: not a valid row"),
        ("1.0.1.0,2001:db8::,CN", "line 2: a range mixes IPv4 and IPv6"),
        ("1.0.0.128,1.0.1.255,CN", "line 2: the ranges overlap"),
        ("1.0.1.0,1.0.1.255,C;", "line 2: a country code is not valid"),
    ],
    ids=["bad-address", "missing-column", "mixed-versions", "overlap", "bad-country"],
)
def test_dbip_row_that_is_not_valid_fails_with_its_line(
    geoip_server: GeoipServer, tmp_path: Path, row: str, error: str
) -> None:
    geoip_server.dbip(f"1.0.0.0,1.0.0.255,AU\n{row}\n")

    with pytest.raises(GeoipError, match=f"DB-IP {error}"):
        fetch_country_map("dbip-lite", "", tmp_path)


def test_maxmind_data_becomes_the_country_map(geoip_server: GeoipServer, tmp_path: Path) -> None:
    geoip_server.pages["/app/geoip_download"] = maxmind_archive()

    text = fetch_country_map("maxmind", LICENSE_KEY, tmp_path)

    # 2.0.2.0/24 is in Europe but in no country, so it has no entry.
    assert text == "2.0.0.0/23 DE\n2.0.1.0/24 FR\n2a01::/32 FR\n"
    [request] = geoip_server.requests
    assert "edition_id=GeoLite2-Country-CSV" in request
    assert f"license_key={LICENSE_KEY}" in request


def test_maxmind_row_that_is_not_valid_fails_with_its_line(
    geoip_server: GeoipServer, tmp_path: Path
) -> None:
    geoip_server.pages["/app/geoip_download"] = maxmind_archive(BLOCKS_HEADER + "2.0.0/24,1,1\n")

    with pytest.raises(GeoipError, match=r"GeoLite2-Country-Blocks-IPv4\.csv line 2"):
        fetch_country_map("maxmind", LICENSE_KEY, tmp_path)


@pytest.mark.parametrize("status", [401, 500])
def test_refused_download_names_the_status_and_never_the_license_key(
    geoip_server: GeoipServer, tmp_path: Path, status: int
) -> None:
    geoip_server.pages["/app/geoip_download"] = maxmind_archive()
    geoip_server.status = status

    with pytest.raises(GeoipError) as error:
        fetch_country_map("maxmind", LICENSE_KEY, tmp_path)

    assert str(error.value) == f"127.0.0.1 answered HTTP {status}"


def test_unreachable_server_fails_with_the_host_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOMEPROXY_GEOIP_URL", "http://127.0.0.1:9")

    with pytest.raises(
        GeoipError, match=r"^127\.0\.0\.1 could not be read, check the internet connection$"
    ):
        fetch_country_map("maxmind", LICENSE_KEY, tmp_path)


@pytest.mark.parametrize(("provider", "source"), [("dbip-lite", "DB-IP"), ("maxmind", "MaxMind")])
def test_download_that_unpacks_too_large_fails(
    geoip_server: GeoipServer,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: Provider,
    source: str,
) -> None:
    """A hostile archive must not fill the memory before the entry cap is checked."""
    monkeypatch.setattr(sources, "MAX_UNPACKED", 10)
    geoip_server.dbip(DBIP_ROWS)
    geoip_server.pages["/app/geoip_download"] = maxmind_archive()

    with pytest.raises(GeoipError, match=f"the {source} download unpacks to more than"):
        fetch_country_map(provider, LICENSE_KEY, tmp_path)


def test_file_that_is_not_gzip_fails(geoip_server: GeoipServer, tmp_path: Path) -> None:
    geoip_server.pages[f"/free/dbip-country-lite-{datetime.now(UTC):%Y-%m}.csv.gz"] = b"<html>"

    with pytest.raises(GeoipError, match="the DB-IP download is not a valid gzip CSV file"):
        fetch_country_map("dbip-lite", "", tmp_path)
