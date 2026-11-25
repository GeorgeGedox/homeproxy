"""Which routes get requests from a Cloudflare edge address, and which of them are not marked."""

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from homeproxy.logs.cloudflare_edge import RELOAD_SECONDS, WARNING_SECONDS, CloudflareEdge
from homeproxy.logs.parser import AccessLog, parse_message

EDGE = "172.64.0.7"
LIVE = ("app", "nas")


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def line(client_ip: str, route: str = "app") -> AccessLog:
    message = (
        f"{client_ip}:60858 [09/Nov/2026:17:08:11.058] fe_sni route-{route}/s 1/0/2 2057 -- "
        f'2/1/0/0/0 0/0 "{route}" "" "{route}.example.com"'
    )
    entry = parse_message(message, datetime(2026, 11, 9, tzinfo=UTC))
    assert entry is not None
    return entry


@pytest.fixture
def release(tmp_path: Path) -> Path:
    """A release that marks `nas` proxied."""
    (tmp_path / "proxied-only.map").write_text("route-nas 1\n")
    (tmp_path / "cloudflare.lst").write_text(
        "# ranges\n172.64.0.0/13\nnot a range\n2606:4700::/32\n"
    )
    return tmp_path


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def edge(release: Path, clock: Clock) -> CloudflareEdge:
    return CloudflareEdge(release / "cloudflare.lst", clock)


@pytest.mark.parametrize(
    ("client_ip", "route", "warned"),
    [
        pytest.param(EDGE, "app", ["app"], id="edge-address"),
        pytest.param("2606:4700::1", "app", ["app"], id="edge-ipv6-address"),
        pytest.param("198.51.100.9", "app", [], id="visitor-address"),
        pytest.param(EDGE, "", [], id="no-route"),
        pytest.param(EDGE, "nas", [], id="route-marked-proxied"),
        pytest.param(EDGE, "gone", [], id="route-not-in-haproxy"),
    ],
)
def test_a_route_warns_when_an_edge_address_reaches_it_unmarked(
    edge: CloudflareEdge, release: Path, client_ip: str, route: str, warned: list[str]
) -> None:
    edge(line(client_ip, route))

    assert edge.unmarked(release, LIVE) == warned


def test_a_route_stops_warning_24_hours_after_the_last_edge_request(
    edge: CloudflareEdge, release: Path, clock: Clock
) -> None:
    edge(line(EDGE))
    clock.now += WARNING_SECONDS - 1
    assert edge.unmarked(release, LIVE) == ["app"]

    clock.now += 2
    assert edge.unmarked(release, LIVE) == []


def test_a_changed_range_list_is_read_again_after_the_check_interval(
    edge: CloudflareEdge, release: Path, clock: Clock
) -> None:
    edge(line("198.51.100.9"))
    ranges = release / "cloudflare.lst"
    ranges.write_text("198.51.100.0/24\n")
    stat = ranges.stat()
    os.utime(ranges, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1))

    edge(line("198.51.100.9"))
    assert edge.unmarked(release, LIVE) == []

    clock.now += RELOAD_SECONDS
    edge(line("198.51.100.9"))
    assert edge.unmarked(release, LIVE) == ["app"]


def test_a_missing_range_list_warns_about_nothing(release: Path, clock: Clock) -> None:
    edge = CloudflareEdge(release / "missing.lst", clock)

    edge(line(EDGE))

    assert edge.unmarked(release, LIVE) == []
