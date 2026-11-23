"""The heartbeat against a real HAProxy: a local receiver gets a ping each interval while HAProxy
answers on its socket, and none once HAProxy is stopped.

The job is driven tick by tick on a hand-moved clock, so the test never waits for a real interval.
"""

import shutil
from pathlib import Path

from homeproxy.jobs.heartbeat import Heartbeat
from homeproxy.store import Store, load_tree, write_tree
from tests.conftest import Receiver
from tests.integration.haproxy_live import LiveHAProxy, docker

DATA = Path(__file__).parents[4] / "examples" / "data"


def heartbeat_store(root: Path, url: str) -> Store:
    """The example data with the heartbeat on at the minimum interval, pinging `url`."""
    shutil.copytree(DATA, root)
    store = Store(root)
    tree = load_tree(store.config_path)
    tree["notifications"] = {"heartbeat": {"enabled": True, "interval": "30s"}}
    write_tree(store.config_path, tree)
    with store.secrets_path.open("a") as secrets:
        secrets.write(f'notifications:\n  heartbeat_url: "{url}"\n')
    return store


def test_pings_at_the_interval_and_none_while_haproxy_is_stopped(
    live: LiveHAProxy, receiver: Receiver, tmp_path: Path
) -> None:
    # Each run reads the clock once: these are the seconds at each tick.
    ticks = iter([0.0, 15.0, 30.0, 60.0, 90.0])
    job = Heartbeat(
        heartbeat_store(tmp_path / "data", receiver.url("/ping")),
        live.client.show_info,
        clock=ticks.__next__,
    )
    for _ in range(3):
        job.run()
    assert receiver.pinged == ["/ping", "/ping"]

    docker("stop", live.container)
    for _ in range(2):
        job.run()

    assert receiver.pinged == ["/ping", "/ping"]
