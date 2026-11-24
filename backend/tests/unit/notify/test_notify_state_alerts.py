"""State alerts: a route, the bouncer or AppSec down after the delay and recovered, one expiring
message per certificate per renewal, none repeated after a restart, and no alert for a disabled
route."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest

from homeproxy.certs.bundle import CertificateInfo, ExpiryState
from homeproxy.haproxy.stats import StatRow, parse_stat
from homeproxy.jobs.state_alerts import STATE_FILE, StateAlerts
from homeproxy.store import Store, load_tree, write_tree
from tests.conftest import Receiver

START = datetime(2026, 11, 1, tzinfo=UTC)


class Clock:
    """A wall clock the test sets in seconds after `START`."""

    seconds = 0.0

    def __call__(self) -> datetime:
        return START + timedelta(seconds=self.seconds)


class Stats:
    """Stands in for `show stat`: each backend in `backends` with its status."""

    def __init__(self) -> None:
        self.backends: dict[str, str] = {}

    def __call__(self) -> list[StatRow]:
        rows = "".join(f"{name},BACKEND,1,{status},\n" for name, status in self.backends.items())
        return parse_stat("# pxname,svname,type,status,\n" + rows)


def appsec_of(stats: Stats) -> Callable[[], Literal["up", "down"] | None]:
    """AppSec's state, from a backend named `appsec` in `stats`, which HAProxy never has."""
    states: dict[str, Literal["up", "down"]] = {"UP": "up", "DOWN": "down"}
    return lambda: states.get(stats.backends.get("appsec", ""))


def messages(receiver: Receiver) -> list[tuple[str, str, str]]:
    Receiver.wait()
    return [(b["event"], b["title"], b["severity"]) for b in receiver.bodies("/hook")]


def no_certificates() -> list[CertificateInfo]:
    return []


@pytest.mark.parametrize(
    ("backend", "event", "down", "recovered"),
    [
        ("route-jellyfin", "route_down", "Route jellyfin is down", "Route jellyfin recovered"),
        # No check path: HAProxy's TCP check still marks it down.
        ("route-files", "route_down", "Route files is down", "Route files recovered"),
        ("route-minecraft", "route_down", "Route minecraft is down", "Route minecraft recovered"),
        ("crowdsec-spoa", "bouncer_down", "CrowdSec bouncer is down", "CrowdSec bouncer recovered"),
        ("appsec", "appsec_down", "CrowdSec AppSec is down", "CrowdSec AppSec recovered"),
    ],
)
def test_down_for_the_delay_sends_one_message_and_recovery_one_more(
    store: Store, receiver: Receiver, backend: str, event: str, down: str, recovered: str
) -> None:
    clock, stats = Clock(), Stats()
    job = StateAlerts(
        store,
        stats,
        no_certificates,
        appsec=appsec_of(stats),
        down_after=timedelta(minutes=2),
        clock=clock,
    )

    # A short outage is quiet, and the delay starts again at the next one.
    for clock.seconds, stats.backends[backend] in [(0, "DOWN"), (60, "UP"), (100, "DOWN")]:
        job.run()
    clock.seconds = 219
    job.run()
    assert messages(receiver) == []

    for clock.seconds in (220, 300, 400):
        job.run()
    assert messages(receiver) == [(event, down, "error")]

    stats.backends[backend] = "UP"
    for clock.seconds in (410, 500):
        job.run()
    assert messages(receiver) == [(event, down, "error"), (event, recovered, "info")]


def test_a_restart_repeats_no_message(store: Store, receiver: Receiver) -> None:
    clock, stats = Clock(), Stats()
    stats.backends["route-jellyfin"] = "DOWN"
    job = StateAlerts(store, stats, no_certificates, down_after=timedelta(0), clock=clock)
    job.run()
    assert (store.root / STATE_FILE).is_file()

    StateAlerts(store, stats, no_certificates, down_after=timedelta(0), clock=clock).run()
    stats.backends["route-jellyfin"] = "UP"
    StateAlerts(store, stats, no_certificates, down_after=timedelta(0), clock=clock).run()
    StateAlerts(store, stats, no_certificates, down_after=timedelta(0), clock=clock).run()

    assert [title for _, title, _ in messages(receiver)] == [
        "Route jellyfin is down",
        "Route jellyfin recovered",
    ]


def disable_jellyfin(store: Store) -> None:
    path = store.route_path("jellyfin")
    tree = load_tree(path)
    tree["enabled"] = False
    write_tree(path, tree)


def test_a_disabled_route_never_alerts(store: Store, receiver: Receiver) -> None:
    disable_jellyfin(store)
    clock, stats = Clock(), Stats()
    stats.backends["route-jellyfin"] = "DOWN"
    job = StateAlerts(store, stats, no_certificates, down_after=timedelta(0), clock=clock)
    for clock.seconds in (0, 3600):
        job.run()
    stats.backends["route-jellyfin"] = "UP"
    job.run()

    assert messages(receiver) == []


def certificate(not_after: datetime) -> CertificateInfo:
    return CertificateInfo(
        "example.com", "CN=Test CA", ("example.com",), not_after, 0, ExpiryState.OK
    )


def test_an_expiring_certificate_is_sent_once_per_renewal(store: Store, receiver: Receiver) -> None:
    clock, installed = Clock(), [certificate(START + timedelta(days=8))]
    days = 24 * 3600

    def run() -> None:
        StateAlerts(store, Stats(), lambda: installed, clock=clock).run()

    run()
    for clock.seconds in (1 * days, 2 * days, 3 * days):
        run()
    assert messages(receiver) == [
        ("certificate_expiring", "Certificate example.com expires soon", "warning")
    ]

    installed = [certificate(START + timedelta(days=90))]
    run()
    clock.seconds = 83 * days
    run()
    run()
    assert len(messages(receiver)) == 2
