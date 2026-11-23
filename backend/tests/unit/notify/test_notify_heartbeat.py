"""The heartbeat job pings at the interval, only while HAProxy answers, and never logs its URL."""

import logging
import shutil
from pathlib import Path

import pytest

from homeproxy.haproxy.client import HAProxyConnectionError
from homeproxy.jobs.heartbeat import Heartbeat
from homeproxy.store import Store, load_tree, write_tree
from tests.conftest import Receiver

DATA = Path(__file__).parents[4] / "examples" / "data"


class Clock:
    """A monotonic clock the test moves by hand."""

    now = 0.0

    def __call__(self) -> float:
        return self.now


class Probe:
    """Stands in for `show info`: raises like the client does while HAProxy is down."""

    up = True

    def __call__(self) -> None:
        if not self.up:
            raise HAProxyConnectionError("connection refused")


def set_heartbeat(store: Store, *, enabled: bool, interval: str = "1m") -> None:
    tree = load_tree(store.config_path)
    tree["notifications"] = {"heartbeat": {"enabled": enabled, "interval": interval}}
    write_tree(store.config_path, tree)


@pytest.fixture
def store(tmp_path: Path, receiver: Receiver) -> Store:
    """The example data with the heartbeat on every minute, pinging the receiver."""
    shutil.copytree(DATA, tmp_path / "data")
    store = Store(tmp_path / "data")
    set_heartbeat(store, enabled=True)
    with store.secrets_path.open("a") as secrets:
        secrets.write(f'notifications:\n  heartbeat_url: "{receiver.url("/ping")}"\n')
    return store


def test_pings_once_per_interval_while_on(store: Store, receiver: Receiver) -> None:
    clock = Clock()
    job = Heartbeat(store, Probe(), clock=clock)

    for clock.now in (0, 10, 50, 60, 100):
        job.run()
    assert receiver.pinged == ["/ping", "/ping"]

    set_heartbeat(store, enabled=False)
    clock.now = 500
    job.run()
    assert len(receiver.pinged) == 2


def test_no_ping_while_haproxy_does_not_answer(store: Store, receiver: Receiver) -> None:
    """The first tick after HAProxy answers again pings at once."""
    clock, probe = Clock(), Probe()
    job = Heartbeat(store, probe, clock=clock)
    job.run()

    probe.up = False
    for clock.now in (60, 120, 180):
        job.run()
    assert receiver.pinged == ["/ping"]

    probe.up = True
    clock.now = 185
    job.run()
    assert receiver.pinged == ["/ping", "/ping"]


def test_a_failed_ping_is_logged_once_without_the_url(
    store: Store, receiver: Receiver, caplog: pytest.LogCaptureFixture
) -> None:
    """No retry: the next interval is the retry."""
    clock = Clock()
    job = Heartbeat(store, Probe(), clock=clock)
    receiver.status = 500
    with caplog.at_level(logging.INFO):
        for clock.now in (0, 30, 60):
            job.run()
        receiver.status = 200
        clock.now = 120
        job.run()

    assert len(receiver.pinged) == 3
    assert caplog.messages == ["heartbeat: HTTP 500", "heartbeat: pings arrive again"]
    assert str(receiver.port) not in caplog.text


def test_nothing_is_logged_before_setup(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Without `config.yaml` the heartbeat is off, which is not a problem to log."""
    with caplog.at_level(logging.WARNING):
        Heartbeat(Store(tmp_path), Probe()).run()
    assert caplog.records == []
