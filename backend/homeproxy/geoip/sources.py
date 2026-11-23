"""Download and read the two GeoIP sources: DB-IP Lite (no account) and MaxMind GeoLite2 (key)."""

import csv
import gzip
import http.client
import io
import ipaddress
import os
import tempfile
import urllib.error
import urllib.parse
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import IO, Literal

from homeproxy.geoip.country_map import UNKNOWN, GeoipError, Ranges
from homeproxy.util import http as http_request

Provider = Literal["dbip-lite", "maxmind"]

URL_ENV = "HOMEPROXY_GEOIP_URL"
DOWNLOAD_PREFIX = ".download-"
"""Replaces the scheme and host of both download URLs, for example `http://127.0.0.1:9`. Tests
set it, so they never reach the internet."""
DEFAULT_URLS: dict[Provider, str] = {
    "dbip-lite": "https://download.db-ip.com",
    "maxmind": "https://download.maxmind.com",
}
FETCH_TIMEOUT = 120.0
MAX_DOWNLOAD = 128 * 1024 * 1024
"""Both downloads are under 10 MB. The cap stops a runaway download from filling the disk."""
MAX_UNPACKED = 256 * 1024 * 1024
"""Twice the download cap. The cap stops a hostile archive from filling the memory."""

_MAXMIND_BLOCKS = {4: "GeoLite2-Country-Blocks-IPv4.csv", 6: "GeoLite2-Country-Blocks-IPv6.csv"}
_MAXMIND_LOCATIONS = "GeoLite2-Country-Locations-en.csv"


@dataclass(frozen=True)
class Attribution:
    """The credit a provider's license requires wherever its data is shown."""

    text: str
    url: str


ATTRIBUTION: dict[Provider, Attribution] = {
    # CC BY 4.0: the UI must link to DB-IP.
    "dbip-lite": Attribution("IP Geolocation by DB-IP", "https://db-ip.com"),
    "maxmind": Attribution(
        "This product includes GeoLite2 data created by MaxMind, available from "
        "https://www.maxmind.com",
        "https://www.maxmind.com",
    ),
}


class _HttpStatusError(GeoipError):
    def __init__(self, host: str, status: int) -> None:
        super().__init__(f"{host} answered HTTP {status}")
        self.status = status


def fetch_ranges(provider: Provider, license_key: str, directory: Path) -> tuple[Ranges, Ranges]:
    """Download the provider's newest data and return its IPv4 and IPv6 ranges.

    The download goes to a temporary file in `directory`, which is removed afterwards. Raises
    `GeoipError` with a message that never holds the license key.
    """
    with tempfile.NamedTemporaryFile(dir=directory, prefix=DOWNLOAD_PREFIX) as file:
        if provider == "maxmind":
            _download(_maxmind_url(license_key), file)
            ipv4, ipv6 = read_maxmind(Path(file.name))
        else:
            _download_dbip(file)
            ipv4, ipv6 = read_dbip(Path(file.name))
    return ipv4, ipv6


def read_dbip(path: Path) -> tuple[Ranges, Ranges]:
    """Read DB-IP's country CSV (gzip): `start,end,country` per line, in address order."""
    families = {4: Ranges(32), 6: Ranges(128)}
    try:
        with (
            gzip.open(path, "rb") as raw,
            io.TextIOWrapper(raw, encoding="ascii", newline="") as file,
        ):
            for number, row in enumerate(csv.reader(file), start=1):
                _check_unpacked("DB-IP", raw.tell())
                with _row_of("DB-IP", number):
                    start_text, end_text, country = row
                    start, end = ipaddress.ip_address(start_text), ipaddress.ip_address(end_text)
                    if start.version != end.version:
                        raise GeoipError("a range mixes IPv4 and IPv6")
                    families[start.version].add(int(start), int(end), country)
    except (OSError, EOFError, UnicodeDecodeError, csv.Error):
        raise GeoipError("the DB-IP download is not a valid gzip CSV file") from None
    return families[4], families[6]


def read_maxmind(path: Path) -> tuple[Ranges, Ranges]:
    """Read MaxMind's GeoLite2 Country CSV archive. A network takes the country where it is,
    or else the country it is registered in."""
    families = {4: Ranges(32), 6: Ranges(128)}
    try:
        with zipfile.ZipFile(path) as archive:
            # zipfile reads no more than the size each member declares.
            _check_unpacked("MaxMind", sum(info.file_size for info in archive.infolist()))
            members = {PurePosixPath(name).name: name for name in archive.namelist()}
            with _rows(archive, members[_MAXMIND_LOCATIONS]) as rows:
                countries = {row["geoname_id"]: row["country_iso_code"] for row in rows}
            for version, name in _MAXMIND_BLOCKS.items():
                with _rows(archive, members[name]) as rows:
                    for number, row in enumerate(rows, start=2):
                        with _row_of(name, number):
                            network = ipaddress.ip_network(row["network"])
                            if network.version != version:
                                raise GeoipError("a network of the other IP version")
                            geoname = row["geoname_id"] or row["registered_country_geoname_id"]
                            country = countries.get(geoname) or UNKNOWN
                            start, end = network.network_address, network.broadcast_address
                            families[version].add(int(start), int(end), country)
    except (OSError, KeyError, UnicodeDecodeError, zipfile.BadZipFile, csv.Error):
        raise GeoipError(
            "the MaxMind download is not a valid GeoLite2 Country CSV archive"
        ) from None
    return families[4], families[6]


def _check_unpacked(source: str, size: int) -> None:
    if size > MAX_UNPACKED:
        raise GeoipError(f"the {source} download unpacks to more than {MAX_UNPACKED >> 20} MB")


@contextmanager
def _rows(archive: zipfile.ZipFile, name: str) -> Iterator[csv.DictReader[str]]:
    with archive.open(name) as raw, io.TextIOWrapper(raw, encoding="utf-8", newline="") as text:
        yield csv.DictReader(text)


@contextmanager
def _row_of(source: str, line: int) -> Iterator[None]:
    """Name the line of a row that is not valid."""
    try:
        yield
    except (ValueError, KeyError, GeoipError) as error:
        reason = str(error) if isinstance(error, GeoipError) else "not a valid row"
        raise GeoipError(f"{source} line {line}: {reason}") from None


def _base_url(provider: Provider) -> str:
    return os.environ.get(URL_ENV, DEFAULT_URLS[provider]).rstrip("/")


def _maxmind_url(license_key: str) -> str:
    query = urllib.parse.urlencode(
        {"edition_id": "GeoLite2-Country-CSV", "license_key": license_key, "suffix": "zip"}
    )
    return f"{_base_url('maxmind')}/app/geoip_download?{query}"


def _download_dbip(file: IO[bytes]) -> None:
    """DB-IP names its file by month and publishes it early in the month. Until then, the file
    of the month before is the newest one."""
    today = datetime.now(UTC).date()
    months = (today, date(today.year, today.month, 1) - timedelta(days=1))
    for month in months:
        url = f"{_base_url('dbip-lite')}/free/dbip-country-lite-{month:%Y-%m}.csv.gz"
        try:
            _download(url, file)
        except _HttpStatusError as error:
            if error.status != 404 or month is months[-1]:
                raise
        else:
            return


def _download(url: str, file: IO[bytes]) -> None:
    """Write the body at `url` to `file`. Errors name only the host, because MaxMind's URL holds
    the license key."""
    host = urllib.parse.urlsplit(url).hostname or "the GeoIP server"
    file.seek(0)
    file.truncate()
    size = 0
    try:
        with http_request.urlopen(url, timeout=FETCH_TIMEOUT) as response:
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_DOWNLOAD:
                    raise GeoipError(f"the download from {host} is over {MAX_DOWNLOAD} bytes")
                file.write(chunk)
    except urllib.error.HTTPError as error:
        raise _HttpStatusError(host, error.code) from None
    except OSError:
        raise GeoipError(f"{host} could not be read, check the internet connection") from None
    except (ValueError, http.client.HTTPException):
        raise GeoipError(f"{host} sent an answer HomeProxy could not read") from None
    file.flush()
