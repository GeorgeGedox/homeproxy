"""The refresh job around the pipeline: what it applies, what it leaves to the admin's next apply,
and what it restores when an apply fails.

The applies that succeed run against a real HAProxy in `tests/integration/geoip/`. Here the
pipeline's `haproxy -c` is `false`, so any apply fails at the check.
"""

import logging
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from homeproxy.geoip.country_map import GeoipError
from homeproxy.geoip.data import (
    DATA_DIR,
    GeoipData,
    load_country_map,
    load_data_info,
    save_country_map,
)
from homeproxy.geoip.lookup import CountryLookup
from homeproxy.geoip.sources import URL_ENV
from homeproxy.haproxy.client import HAProxyClient
from homeproxy.haproxy.pipeline import Pipeline
from homeproxy.jobs import JobRunner
from homeproxy.jobs import geoip as geoip_job
from homeproxy.jobs.geoip import (
    CHECK_INTERVAL,
    INTERVAL,
    refresh,
    refresh_if_due,
    refresh_on_schedule,
)
from homeproxy.notify import sender
from homeproxy.store import Store, load_tree, write_tree
from homeproxy.store.pending import PendingChanges
from homeproxy.util.run import RunResult, run
from tests.conftest import Receiver
from tests.unit.geoip.conftest import GeoipServer

DATA = Path(__file__).parents[4] / "examples" / "data"
NEW_MAP = "1.0.0.0/24 AU\n"
OLD_MAP = "1.0.0.0/24 CN\n"
NEW_ROUTE = b"""version: 1
name: new
type: http
hosts: [new.example.com]
exposure: lan
upstreams:
  - address: 192.168.1.50:80
"""
LICENSE_KEY = "maxmind-license-key-value"
OLD_INFO = GeoipData(
    provider="dbip-lite", updated_at=datetime(2026, 10, 1, tzinfo=UTC), entries=1, countries=["CN"]
)


@pytest.fixture
def pipeline(tmp_path: Path, geoip_server: GeoipServer) -> Pipeline:
    shutil.copytree(DATA, tmp_path / "data")
    (tmp_path / "data" / "releases" / "placeholder").mkdir(parents=True)
    (tmp_path / "data" / "generated").symlink_to("releases/placeholder")
    geoip_server.dbip("1.0.0.0,1.0.0.255,AU\n")
    missing = str(tmp_path / "missing.sock")
    client = HAProxyClient(missing, missing)
    return Pipeline(Store(tmp_path / "data"), client=client, haproxy_command=("false",))


def test_failed_apply_restores_the_old_map(pipeline: Pipeline) -> None:
    root = pipeline.store.root
    save_country_map(root, OLD_MAP, OLD_INFO)

    with pytest.raises(GeoipError, match="could not be applied: haproxy -c refused"):
        refresh(pipeline)

    assert (load_country_map(root), load_data_info(root)) == (OLD_MAP, OLD_INFO)


def test_failed_apply_keeps_an_old_map_whose_description_is_outdated(pipeline: Pipeline) -> None:
    """A description from before the code list loads as None. The map in use must survive, and
    the missing description makes the next check download again."""
    root = pipeline.store.root
    save_country_map(root, OLD_MAP, OLD_INFO)
    (root / DATA_DIR / "country.json").write_text(OLD_INFO.model_dump_json(exclude={"countries"}))

    with pytest.raises(GeoipError, match="could not be applied"):
        refresh(pipeline)

    assert (load_country_map(root), load_data_info(root)) == (OLD_MAP, None)


def test_first_map_whose_apply_fails_is_removed(pipeline: Pipeline) -> None:
    with pytest.raises(GeoipError, match="could not be applied"):
        refresh(pipeline)

    assert (load_country_map(pipeline.store.root), load_data_info(pipeline.store.root)) == (
        "",
        None,
    )


def test_pending_edits_leave_the_new_map_to_the_next_apply(pipeline: Pipeline) -> None:
    with pipeline.store.lock() as held:
        PendingChanges(pipeline.store).stage("routes/new.yaml", NEW_ROUTE, held)

    refresh(pipeline)

    assert load_country_map(pipeline.store.root) == NEW_MAP
    assert [path.name for path in (pipeline.store.root / "releases").iterdir()] == ["placeholder"]
    # The log collector's data does not wait for an apply.
    assert CountryLookup(pipeline.store.root).country("1.0.0.1") == "AU"


def test_the_description_lists_the_codes_a_save_may_block(
    pipeline: Pipeline, geoip_server: GeoipServer
) -> None:
    """ZZ, the data's "no country", is in the map as a hole inside AU, but it is no country."""
    geoip_server.dbip("1.0.0.0,1.0.0.63,AU\n1.0.0.64,1.0.0.127,ZZ\n1.0.0.128,1.0.0.255,AU\n")
    with pipeline.store.lock() as held:
        PendingChanges(pipeline.store).stage("routes/new.yaml", NEW_ROUTE, held)

    refresh(pipeline)

    info = load_data_info(pipeline.store.root)
    assert load_country_map(pipeline.store.root) == "1.0.0.0/24 AU\n1.0.0.64/26 ZZ\n"
    assert info is not None
    assert (info.entries, info.countries) == (2, ["AU"])


def test_map_that_did_not_change_is_not_applied_again(pipeline: Pipeline) -> None:
    root = pipeline.store.root
    save_country_map(root, NEW_MAP, OLD_INFO)

    refresh(pipeline)

    info = load_data_info(root)
    assert info is not None
    assert info.updated_at > OLD_INFO.updated_at


def test_maxmind_without_a_license_key_fails(pipeline: Pipeline) -> None:
    config = pipeline.store.config_path
    config.write_text(config.read_text().replace("provider: dbip-lite", "provider: maxmind"))

    with pytest.raises(GeoipError, match="MaxMind needs a license key"):
        refresh(pipeline)


def test_child_error_keeps_its_safe_message_and_the_key_never_reaches_argv(
    pipeline: Pipeline, geoip_server: GeoipServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, secrets = pipeline.store.config_path, pipeline.store.secrets_path
    config.write_text(config.read_text().replace("provider: dbip-lite", "provider: maxmind"))
    key_line = f'maxmind_license_key: "{LICENSE_KEY}"'
    secrets.write_text(secrets.read_text().replace('maxmind_license_key: ""', key_line))
    geoip_server.pages["/app/geoip_download"], geoip_server.status = b"zip", 403
    argvs: list[list[str]] = []

    def spy(argv: list[str], **options: Any) -> RunResult:
        argvs.append(argv)
        return run(argv, **options)

    monkeypatch.setattr(geoip_job, "run", spy)

    with pytest.raises(GeoipError, match=r"^127\.0\.0\.1 answered HTTP 403$"):
        refresh(pipeline)

    assert [LICENSE_KEY in request for request in geoip_server.requests] == [True]
    assert [LICENSE_KEY in " ".join(argv) for argv in argvs] == [False]


def test_a_build_that_times_out_leaves_no_download_behind(
    pipeline: Pipeline, hanging_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The child is killed, so it cannot remove its own download, which can be 128 MB. The
    hourly retries would otherwise fill the disk."""
    monkeypatch.setenv(URL_ENV, hanging_url.removesuffix("/hook"))
    monkeypatch.setattr(geoip_job, "BUILD_TIMEOUT", 1.0)

    with pytest.raises(GeoipError, match="did not finish"):
        refresh(pipeline)

    assert list((pipeline.store.root / DATA_DIR).glob(".download-*")) == []


NOW = datetime(2026, 11, 12, tzinfo=UTC)


def queued_refreshes(runner: JobRunner) -> int:
    """How many refresh jobs the checks queued. The runner is not started, so none ran."""
    return sum(job.kind == geoip_job.KIND for job in runner.tracker.jobs())


@pytest.mark.parametrize(
    ("age", "due"),
    [(None, True), (INTERVAL - timedelta(days=1), False), (INTERVAL, True)],
    ids=["no-data", "fresh-data", "old-data"],
)
def test_the_hourly_check_refreshes_missing_or_old_data(
    pipeline: Pipeline, age: timedelta | None, due: bool
) -> None:
    if age is not None:
        info = OLD_INFO.model_copy(update={"updated_at": NOW - age})
        save_country_map(pipeline.store.root, OLD_MAP, info)
    runner = JobRunner()

    refresh_if_due(runner, pipeline, NOW)

    assert queued_refreshes(runner) == int(due)


def test_the_check_does_nothing_before_setup(
    pipeline: Pipeline, caplog: pytest.LogCaptureFixture
) -> None:
    pipeline.store.config_path.unlink()
    runner = JobRunner()
    caplog.set_level(logging.DEBUG)

    refresh_if_due(runner, pipeline, NOW)

    assert queued_refreshes(runner) == 0
    assert not caplog.records


def test_a_failed_refresh_is_queued_again_at_the_next_check(pipeline: Pipeline) -> None:
    info = OLD_INFO.model_copy(update={"updated_at": NOW - INTERVAL})
    save_country_map(pipeline.store.root, OLD_MAP, info)
    runner = JobRunner()
    refresh_if_due(runner, pipeline, NOW)
    with pytest.raises(GeoipError):
        refresh(pipeline)

    refresh_if_due(runner, pipeline, NOW + CHECK_INTERVAL)

    assert queued_refreshes(runner) == 2


def set_provider(store: Store, provider: str) -> None:
    tree = load_tree(store.config_path)
    tree["geoip"]["provider"] = provider
    write_tree(store.config_path, tree)


def test_scheduled_retries_send_one_message_per_run_of_failures(
    pipeline: Pipeline, receiver: Receiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MaxMind without a key fails; dbip-lite with the map in use succeeds without an apply. A
    message no target took is sent again at the next failure."""
    monkeypatch.setattr(sender, "RETRIES", 0)
    store = pipeline.store
    tree = load_tree(store.config_path)
    tree["notifications"] = {"targets": [{"name": "hook", "type": "webhook"}]}
    write_tree(store.config_path, tree)
    with store.secrets_path.open("a") as secrets:
        secrets.write(f'notifications:\n  targets:\n    hook: {{url: "{receiver.url("/hook")}"}}\n')
    save_country_map(store.root, NEW_MAP, OLD_INFO)

    def sent() -> int:
        Receiver.wait()
        return len(receiver.bodies("/hook"))

    set_provider(store, "maxmind")
    receiver.status = 500
    with pytest.raises(GeoipError):
        refresh_on_schedule(pipeline)
    assert sent() == 1
    receiver.status = 204
    for _ in range(2):
        with pytest.raises(GeoipError):
            refresh_on_schedule(pipeline)
        # Runs are an hour apart, so each one's message is through before the next starts.
        Receiver.wait()
    assert sent() == 2

    set_provider(store, "dbip-lite")
    refresh_on_schedule(pipeline)
    set_provider(store, "maxmind")
    with pytest.raises(GeoipError):
        refresh_on_schedule(pipeline)
    assert sent() == 3
