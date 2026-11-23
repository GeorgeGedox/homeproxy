"""The reconcile schedule reads its interval from the applied config."""

import logging
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import pytest

from homeproxy.cloudflare.reconcile import ReconcileError
from homeproxy.jobs import JobRunner
from homeproxy.jobs.reconcile import Reconcile
from homeproxy.store import Store


@pytest.mark.parametrize(
    ("config", "warned"),
    [
        pytest.param(None, False, id="before-setup"),
        pytest.param("cloudflare: [", True, id="broken"),
    ],
)
def test_a_config_that_cannot_be_read_gets_the_default_interval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    config: str | None,
    warned: bool,
) -> None:
    """The API must still start. Before setup there is no `config.yaml`, which is no problem to
    log; a broken one is."""
    intervals: list[timedelta] = []

    def schedule_every(
        self: JobRunner, kind: str, func: Callable[[], object], *, interval: timedelta, **_: object
    ) -> None:
        intervals.append(interval)

    monkeypatch.setattr(JobRunner, "schedule_every", schedule_every)
    store = Store(tmp_path)
    if config is not None:
        store.config_dir.mkdir()
        store.config_path.write_text(config)
    with caplog.at_level(logging.WARNING):
        Reconcile(store).schedule(JobRunner())
    assert intervals == [timedelta(minutes=15)]
    assert bool(caplog.records) == warned


def test_a_run_before_setup_changes_nothing(tmp_path: Path) -> None:
    """The scheduled run does not fail while there is no `config.yaml`."""
    assert Reconcile(Store(tmp_path)).run().zones == []


def test_a_broken_config_is_a_refused_run(tmp_path: Path) -> None:
    """The Cloudflare page shows the last run, so a hand edit that breaks `config.yaml` must not
    leave an older "in sync" there."""
    store = Store(tmp_path)
    store.config_dir.mkdir()
    store.config_path.write_text("cloudflare: [")
    reconcile = Reconcile(store)

    with pytest.raises(ReconcileError):
        reconcile.run()

    assert reconcile.last is not None
    assert "config.yaml" in reconcile.last.error
