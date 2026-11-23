"""The monthly GeoIP refresh: download the configured provider's data and build `country.map`."""

import logging
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from homeproxy.geoip.__main__ import KEY_ENV
from homeproxy.geoip.country_map import UNKNOWN, GeoipError
from homeproxy.geoip.data import (
    DATA_DIR,
    GeoipData,
    load_country_map,
    load_data_info,
    save_country_map,
)
from homeproxy.geoip.sources import DOWNLOAD_PREFIX, Provider
from homeproxy.haproxy.pipeline import Pipeline
from homeproxy.jobs.runner import JobRunner
from homeproxy.jobs.tracker import error_text
from homeproxy.notify import mark_delivered, notify, notify_on_failure
from homeproxy.store import Store
from homeproxy.util.atomic import ensure_dir
from homeproxy.util.run import CommandFailed, CommandTimedOut, RunError, run

KIND = "geoip.refresh"
INTERVAL = timedelta(days=30)
"""The age at which the saved data is refreshed."""
CHECK_INTERVAL = timedelta(hours=1)
FIRST_RUN_DELAY = timedelta(minutes=1)
AUDIT_USER = "geoip-refresh"
BUILD_TIMEOUT = 600.0
"""Covers two downloads, each allowed `FETCH_TIMEOUT` without data, and the build."""
TITLE = "GeoIP refresh failed"
FAILING_FILE = Path("state") / "geoip-failing"
"""Present while the scheduled refresh fails, so its retries and a restart send no new message."""

logger = logging.getLogger(__name__)


def refresh(pipeline: Pipeline) -> None:
    """Fetch new data, save the map it builds and apply it, unless it is the map in use.

    The map goes live at once when no edits are pending, and with the admin's next apply
    otherwise. If that apply fails, the old map is restored, so it cannot block later applies.
    Raises `GeoipError` with a message that is safe to show. Each failure sends a `geoip_failed`
    notification.
    """
    notify_on_failure(pipeline.store, "geoip_failed", TITLE, lambda: _refresh(pipeline))
    _report(pipeline.store, "")


def refresh_on_schedule(pipeline: Pipeline) -> None:
    """Refresh as `refresh` does, but send `geoip_failed` only when a run of failures starts,
    not for each hourly retry."""
    try:
        _refresh(pipeline)
    except Exception as error:
        _report(pipeline.store, error_text(error))
        raise
    _report(pipeline.store, "")


def _report(store: Store, failure: str) -> None:
    path = store.root / FAILING_FILE
    try:
        if not failure:
            path.unlink(missing_ok=True)
            return
        if path.exists():
            return
        # Marked only once a target took it, so a message lost to an outage is sent again.
        # shortcut: a success while this send is still retrying is undone when it lands, and the
        # next run of failures stays silent; track the run in the marker if that is ever seen.
        notify(store, "geoip_failed", TITLE, failure, delivered=lambda: mark_delivered(path))
    except OSError as error:
        logger.error("cannot record the GeoIP refresh state: %s", error)


def _refresh(pipeline: Pipeline) -> None:
    store = pipeline.store
    provider = store.load_config().geoip.provider
    key = ""
    if provider == "maxmind":
        key = store.load_secrets().geoip.maxmind_license_key.get_secret_value()
        if not key:
            raise GeoipError("MaxMind needs a license key in secrets.yaml")
    ensure_dir(store.root / DATA_DIR)
    text = _fetch_in_child(provider, key, store.root / DATA_DIR)
    info = GeoipData(
        provider=provider,
        updated_at=datetime.now(UTC),
        entries=text.count("\n"),
        countries=sorted({line.rpartition(" ")[2] for line in text.splitlines()} - {UNKNOWN}),
    )
    with store.lock():
        old_text, old_info = load_country_map(store.root), load_data_info(store.root)
        save_country_map(store.root, text, info)
    if text == old_text:
        return
    result = pipeline.apply_unless_pending(AUDIT_USER)
    if result is None or result.success:
        return
    with store.lock():
        save_country_map(store.root, old_text, old_info)
    raise GeoipError(f"the new country map could not be applied: {'; '.join(result.errors)}")


def _fetch_in_child(provider: Provider, key: str, directory: Path) -> str:
    """Download and build the map in a child process. In the API process, the memory of the
    build would stay fragmented after the build, about 20 MB of the idle footprint."""
    argv = [sys.executable, "-m", "homeproxy.geoip", provider, str(directory)]
    try:
        return run(argv, timeout=BUILD_TIMEOUT, secret_env={KEY_ENV: key}).stdout
    except CommandFailed as error:
        lines = error.stderr.strip().splitlines()
        raise GeoipError(
            lines[-1] if lines else f"the map build exited with {error.returncode}"
        ) from None
    except CommandTimedOut as error:
        # The killed child cannot remove its download, which can be 128 MB.
        for leftover in directory.glob(f"{DOWNLOAD_PREFIX}*"):
            leftover.unlink(missing_ok=True)
        raise GeoipError(f"the map build did not finish within {error.timeout:g} s") from None
    except RunError as error:
        logger.error("cannot start the GeoIP map build: %s", error)
        raise GeoipError("the map build could not start") from None


def refresh_if_due(runner: JobRunner, pipeline: Pipeline, now: datetime) -> None:
    """Queue a refresh job when the saved data is missing or older than `INTERVAL`.

    Does nothing before setup, because the provider is in `config.yaml`. A failed refresh leaves
    the old data, so the next check queues it again.
    """
    store = pipeline.store
    if not store.configured():
        return
    info = load_data_info(store.root)
    if info is not None and now - info.updated_at < INTERVAL:
        return
    runner.submit(KIND, lambda: refresh_on_schedule(pipeline))


def schedule_refresh(runner: JobRunner, pipeline: Pipeline) -> None:
    """Check every hour, from a minute after start, whether the data is due for a refresh."""
    runner.schedule_every(
        KIND,
        lambda: refresh_if_due(runner, pipeline, datetime.now(UTC)),
        interval=CHECK_INTERVAL,
        first_run=datetime.now(UTC) + FIRST_RUN_DELAY,
        track=False,
    )
