"""Route alerts against a real HAProxy: stopping a route's upstream sends one down message, and
starting it again one recovered message.

The upstream is a second HAProxy in the first one's network namespace, so it keeps its address
across a stop and a start. The job runs with a 1 s delay and is polled until the messages arrive.
"""

import os
import shutil
import time
import uuid
from datetime import timedelta
from pathlib import Path

from homeproxy.haproxy.stats import ProxyType
from homeproxy.jobs.state_alerts import StateAlerts
from homeproxy.store import Store, load_tree, write_tree
from tests.conftest import Receiver
from tests.integration.haproxy_live import (
    CONFIG,
    LiveHAProxy,
    PemPair,
    docker,
    haproxy_image,
    wait_for_haproxy,
)

DATA = Path(__file__).parents[4] / "examples" / "data"
TIMEOUT = 30.0
UPSTREAM_PORT = 9000
"""Inside the HAProxy container's network namespace only, never on the host."""

UPSTREAM = f"""\
defaults
    mode http
    timeout connect 1s
    timeout client 5s
    timeout server 5s

frontend upstream
    bind :{UPSTREAM_PORT}
    http-request return status 200
"""

# The example route `jellyfin` has a health check.
ROUTE_BACKEND = f"""
backend route-jellyfin
    option httpchk GET /health
    server s1 127.0.0.1:{UPSTREAM_PORT} check inter 100ms fall 1 rise 1
"""


def alert_store(root: Path, url: str) -> Store:
    """The example data with one webhook target for every event."""
    shutil.copytree(DATA, root)
    store = Store(root)
    tree = load_tree(store.config_path)
    tree["notifications"] = {"targets": [{"name": "hook", "type": "webhook"}]}
    write_tree(store.config_path, tree)
    with store.secrets_path.open("a") as secrets:
        secrets.write(f'notifications:\n  targets:\n    hook: {{url: "{url}"}}\n')
    return store


def route_status(live: LiveHAProxy) -> str:
    rows = live.client.show_stat()
    backend = (r for r in rows if r.type is ProxyType.BACKEND and r.proxy == "route-jellyfin")
    return next(backend).status


def run_until_sent(job: StateAlerts, receiver: Receiver, count: int) -> None:
    """Run the job until `count` messages arrived, as the scheduler would, only faster."""
    deadline = time.monotonic() + TIMEOUT
    while len(receiver.bodies("/hook")) < count:
        assert time.monotonic() < deadline, f"no message {count} after {TIMEOUT} s"
        job.run()
        time.sleep(0.1)


def test_a_stopped_upstream_sends_one_down_and_one_recovered_message(
    live: LiveHAProxy, certs: tuple[PemPair, PemPair], receiver: Receiver, tmp_path: Path
) -> None:
    (live.data / "upstream.cfg").write_text(UPSTREAM)
    upstream = f"homeproxy-test-upstream-{uuid.uuid4().hex[:12]}"
    try:
        docker(
            "run",
            "--detach",
            "--name",
            upstream,
            "--network",
            f"container:{live.container}",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--volume",
            f"{live.data}:/data:ro",
            haproxy_image(),
            "haproxy",
            "-f",
            "/data/upstream.cfg",
        )
        live.publish_release(CONFIG.format(marker="v2") + ROUTE_BACKEND, certs[0])
        live.client.reload()
        wait_for_haproxy(
            live.container, lambda: route_status(live) == "UP", "the upstream to be up"
        )

        store = alert_store(tmp_path / "data", receiver.url("/hook"))
        job = StateAlerts(store, live.client.show_stat, list, down_after=timedelta(seconds=1))
        job.run()

        docker("stop", "--time", "1", upstream)
        run_until_sent(job, receiver, 1)
        docker("start", upstream)
        run_until_sent(job, receiver, 2)
        for _ in range(5):
            job.run()
    finally:
        docker("rm", "--force", upstream)

    Receiver.wait()
    sent = [(body["event"], body["title"]) for body in receiver.bodies("/hook")]
    assert sent == [
        ("route_down", "Route jellyfin is down"),
        ("route_down", "Route jellyfin recovered"),
    ]
