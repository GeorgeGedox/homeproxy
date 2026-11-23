"""The DDNS job against a fake Cloudflare and a fake WAN address check.

In the seeded examples, `example.com` has `ddns_update: true` and `example.net` has it off.
"""

import logging
import threading
from collections.abc import Callable, Iterator
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from homeproxy.cloudflare import reconcile_store
from homeproxy.cloudflare.ddns import DdnsError, DdnsResult, adopt, update_ddns
from homeproxy.jobs.ddns import FAILING_FILE, KIND, Ddns
from homeproxy.jobs.models import JobState
from homeproxy.jobs.runner import JobRunner
from homeproxy.store import Store, load_tree, write_tree
from tests.conftest import wait_until
from tests.unit.cloudflare.conftest import FakeCloudflare

TAG = "managed-by:homeproxy ddns:example.com"


class FakeWan:
    """Answers every request with `status` and `body`, and counts the requests."""

    def __init__(self) -> None:
        self.status = 200
        self.body = "fl=1f1\nh=1.1.1.1\nip=192.0.2.7\nts=1.2\n"
        self.calls = 0
        self.url = ""


@pytest.fixture
def wan() -> Iterator[FakeWan]:
    fake = FakeWan()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            fake.calls += 1
            raw = fake.body.encode()
            self.send_response(fake.status)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True).start()
    fake.url = f"http://127.0.0.1:{server.server_port}/cdn-cgi/trace"
    yield fake
    server.shutdown()
    server.server_close()


def set_config(store: Store, *path: str | int, value: object) -> None:
    tree = load_tree(store.config_path)
    node = tree
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    write_tree(store.config_path, tree)


def records(fake: FakeCloudflare, zone: str) -> dict[str, tuple[Any, ...]]:
    return {
        r["id"]: (r["type"], r["name"], r["content"], r["comment"])
        for r in fake.zone(zone).values()
    }


def test_ip_change_updates_only_the_ddns_record(
    cloudflare: FakeCloudflare, store: Store, wan: FakeWan
) -> None:
    reconcile_store(store)
    cloudflare.add("example.com", "nas.example.com", content="192.0.2.50", type="A")
    job = Ddns(store, wan.url)

    job.run()
    assert job.results == [DdnsResult("example.com", "home.example.com", "created", "192.0.2.7")]
    before = records(cloudflare, "example.com")
    ddns = next(i for i, r in before.items() if r[1] == "home.example.com")
    assert before[ddns] == ("A", "home.example.com", "192.0.2.7", TAG)
    net_before = records(cloudflare, "example.net")
    cloudflare.calls.clear()

    wan.body = "ip=192.0.2.8\n"
    job.run()

    assert cloudflare.writes() == [f"PUT /zones/id-example.com/dns_records/{ddns}"]
    assert records(cloudflare, "example.com") == before | {
        ddns: ("A", "home.example.com", "192.0.2.8", TAG)
    }
    assert records(cloudflare, "example.net") == net_before
    assert [r.action for r in job.results] == ["updated"]
    cloudflare.calls.clear()

    job.run()
    assert cloudflare.writes() == []
    assert [r.action for r in job.results] == ["unchanged"]


def test_hand_made_ddns_record_changes_only_after_adoption(
    cloudflare: FakeCloudflare, store: Store, wan: FakeWan
) -> None:
    mine = cloudflare.add(
        "example.com", "home.example.com", "my router", type="A", content="192.0.2.1",
        proxied=True, ttl=60,
    )  # fmt: skip
    job = Ddns(store, wan.url)

    job.run()
    assert cloudflare.writes() == []
    assert job.results == [DdnsResult("example.com", "home.example.com", "hand-made", "192.0.2.1")]

    result = adopt(store.load_config(), store.load_secrets(), "example.com")
    assert result.action == "adopted"
    record = cloudflare.zone("example.com")[mine]
    assert (record["content"], record["proxied"], record["ttl"], record["comment"]) == (
        "192.0.2.1", True, 60, TAG,
    )  # fmt: skip

    job.run()
    record = cloudflare.zone("example.com")[mine]
    assert (record["content"], record["proxied"], record["ttl"], record["comment"]) == (
        "192.0.2.7", True, 60, TAG,
    )  # fmt: skip


OWNED = ("A", TAG)
HAND_MADE = ("A", "my router")


@pytest.mark.parametrize(
    ("existing", "action", "writes"),
    [
        pytest.param([HAND_MADE], "hand-made", [], id="hand-made-a"),
        pytest.param([("A", "managed-by:homeproxy ddns:example.com but mine")], "hand-made", [],
                     id="look-alike-suffix"),
        pytest.param([("A", "managed-by:homeproxy ddns:example.org")], "hand-made", [],
                     id="look-alike-zone"),
        pytest.param([("CNAME", "")], "blocked", [], id="cname"),
        pytest.param([HAND_MADE, HAND_MADE], "blocked", [], id="two-hand-made-a"),
        pytest.param([OWNED, HAND_MADE], "blocked", [], id="owned-next-to-hand-made"),
        pytest.param([("MX", ""), ("TXT", ""), ("AAAA", "")], "created", ["POST"],
                     id="other-types-do-not-block"),
        pytest.param([OWNED, OWNED], "updated", ["DELETE", "PUT"], id="two-owned-a"),
    ],
)  # fmt: skip
def test_records_on_the_ddns_name_decide_the_write(
    cloudflare: FakeCloudflare,
    store: Store,
    wan: FakeWan,
    existing: list[tuple[str, str]],
    action: str,
    writes: list[str],
) -> None:
    for number, (kind, comment) in enumerate(existing):
        cloudflare.add(
            "example.com", "home.example.com", comment, type=kind, content=f"192.0.2.{number}"
        )
    job = Ddns(store, wan.url)

    job.run()

    assert [call.split()[0] for call in cloudflare.writes()] == writes
    assert [(r.action, bool(r.error)) for r in job.results] == [(action, action == "blocked")]
    a_records = [r for r in cloudflare.zone("example.com").values() if r["comment"] == TAG]
    if action in ("created", "updated"):
        assert [r["content"] for r in a_records] == ["192.0.2.7"]


@pytest.mark.parametrize(
    ("zone", "count", "message"),
    [
        pytest.param("example.org", 0, "not a configured domain", id="unknown-zone"),
        pytest.param("example.com", 0, "no A record to adopt", id="no-record"),
        pytest.param("example.com", 2, "2 A records", id="blocked"),
    ],
)
def test_adopt_refuses_without_a_single_hand_made_a_record(
    cloudflare: FakeCloudflare, store: Store, zone: str, count: int, message: str
) -> None:
    for number in range(count):
        cloudflare.add(
            "example.com", "home.example.com", "mine", type="A", content=f"192.0.2.{number}"
        )

    with pytest.raises(DdnsError, match=message):
        adopt(store.load_config(), store.load_secrets(), zone)
    assert cloudflare.writes() == []


def test_integration_off_makes_no_calls(
    cloudflare: FakeCloudflare, store: Store, wan: FakeWan
) -> None:
    set_config(store, "cloudflare", "enabled", value=False)
    config, secrets = store.load_config(), store.load_secrets()

    assert update_ddns(config, secrets, wan_url=wan.url) == []
    with pytest.raises(DdnsError, match="integration is off"):
        adopt(config, secrets, "example.com")
    store.secrets_path.unlink()  # the job must not need secrets.yaml while the integration is off
    job = Ddns(store, wan.url)
    job.run()

    assert job.results == []
    assert (cloudflare.calls, wan.calls) == ([], 0)


def test_the_job_is_quiet_before_setup(
    tmp_path: Path, wan: FakeWan, caplog: pytest.LogCaptureFixture
) -> None:
    """Without `config.yaml` there is nothing to update, so the run neither fails nor logs."""
    store = Store(tmp_path)
    with caplog.at_level(logging.WARNING):
        Ddns(store, wan.url).run()
    assert caplog.records == []
    assert wan.calls == 0
    assert not (tmp_path / FAILING_FILE).exists()


def test_a_domain_on_another_provider_gets_no_ddns(
    cloudflare: FakeCloudflare, store: Store, wan: FakeWan
) -> None:
    set_config(store, "domains", 0, "dns_provider", value="route53")
    config, secrets = store.load_config(), store.load_secrets()

    assert update_ddns(config, secrets, wan_url=wan.url) == []
    with pytest.raises(DdnsError, match=r"example\.com uses the DNS provider route53"):
        adopt(config, secrets, "example.com")
    assert (cloudflare.calls, wan.calls) == ([], 0)


def test_fixed_public_ip_skips_the_wan_check(
    cloudflare: FakeCloudflare, store: Store, wan: FakeWan
) -> None:
    set_config(store, "network", "public_ip", value="198.51.100.4")

    Ddns(store, wan.url).run()

    assert wan.calls == 0
    assert [r[2] for r in records(cloudflare, "example.com").values()] == ["198.51.100.4"]


@pytest.mark.parametrize(
    ("status", "body", "message"),
    [
        pytest.param(200, "<html>captive portal</html>", "gave no IPv4 address", id="no-ip"),
        pytest.param(500, "ip=192.0.2.9\n", "answered HTTP 500", id="server-error"),
    ],
)
def test_unknown_wan_address_changes_nothing(
    cloudflare: FakeCloudflare, store: Store, wan: FakeWan, status: int, body: str, message: str
) -> None:
    """A failed check writes nothing and keeps the last results and when they were checked."""
    job = Ddns(store, wan.url)
    job.run()
    last = (job.results, job.checked_at)
    assert last[1] is not None
    cloudflare.calls.clear()
    wan.status, wan.body = status, body

    with pytest.raises(DdnsError, match=message):
        job.run()
    assert cloudflare.calls == []
    assert (job.results, job.checked_at) == last


def test_failing_zone_is_reported_and_the_others_still_run(
    cloudflare: FakeCloudflare, store: Store, wan: FakeWan
) -> None:
    set_config(store, "domains", 1, "cloudflare", "ddns_update", value=True)
    set_config(store, "domains", 1, "credentials", value="cf-main")  # the wrong zone's token

    job = Ddns(store, wan.url)
    job.run()

    assert [(r.zone, r.action) for r in job.results] == [
        ("example.com", "created"),
        ("example.net", "failed"),
    ]
    assert "Authentication error" in job.results[1].error


class RecordingRunner(JobRunner):
    """A real runner that also records the arguments of each schedule."""

    def __init__(self) -> None:
        super().__init__(max_workers=1)
        self.schedules: list[tuple[str, timedelta, bool]] = []

    def schedule_every(  # type: ignore[override]
        self, kind: str, func: Callable[[], None], *, interval: timedelta, track: bool = True
    ) -> None:
        self.schedules.append((kind, interval, track))
        super().schedule_every(kind, func, interval=interval, track=track)


def test_schedule_runs_at_once_then_every_five_minutes_untracked(
    cloudflare: FakeCloudflare, store: Store, wan: FakeWan
) -> None:
    job = Ddns(store, wan.url)
    runner = RecordingRunner()
    try:
        job.schedule(runner)
        runner.start()
        # The results are set before the runner marks the job finished, so wait for the runner.
        done = [(KIND, JobState.FINISHED)]
        jobs = runner.tracker.jobs
        wait_until(lambda: [(j.kind, j.state) for j in jobs()] == done, "the DDNS job", timeout=10)
        assert [r.action for r in job.results] == ["created"]
        assert runner.schedules == [(KIND, timedelta(minutes=5), False)]
    finally:
        runner.shutdown(wait=True)
