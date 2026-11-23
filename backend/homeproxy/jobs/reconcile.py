"""The Cloudflare reconcile job: after every apply, and every `cloudflare.reconcile_interval` while
it is on."""

import logging
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from homeproxy.cloudflare.reconcile import ReconcileError, ZoneResult, reconcile_store
from homeproxy.jobs.runner import JobRunner
from homeproxy.models.config import CloudflareSettings
from homeproxy.store import Store, StoreError

logger = logging.getLogger(__name__)

KIND = "cloudflare.reconcile"


@dataclass(frozen=True)
class ReconcileRun:
    """When reconcile last ran, what it did per zone, or why it refused to run."""

    at: datetime
    zones: list[ZoneResult] = field(default_factory=list)
    error: str = ""


class Reconcile:
    """Runs `reconcile_store` on `/data/config` and keeps the last run in `last`.

    One run at a time: the scheduled run, the run after an apply and a run on request wait for
    each other, so two runs never plan against the same listing.
    """

    def __init__(self, store: Store) -> None:
        self._store = store
        self._lock = threading.Lock()
        self.last: ReconcileRun | None = None

    def run(self) -> ReconcileRun:
        """Reconcile once, keep the run in `last` and return it. Changes nothing while the
        integration is off.

        Raises `ReconcileError` when reconcile refuses to run, after recording it in `last`.
        """
        with self._lock:
            zones: list[ZoneResult] = []
            try:
                if self._enabled():
                    zones = reconcile_store(self._store)
            except ReconcileError as error:
                self.last = ReconcileRun(datetime.now(UTC), error=str(error))
                raise
            self.last = ReconcileRun(datetime.now(UTC), zones)
            return self.last

    def _enabled(self) -> bool:
        """Whether the integration is on. Raises `ReconcileError` if `config.yaml` is broken."""
        try:
            return self._store.configured() and self._store.load_config().cloudflare.enabled
        except StoreError as error:
            raise ReconcileError(
                f"config.yaml cannot be read, so nothing was synced: {error}"
            ) from None

    def run_job(self) -> None:
        """Run once as a job: a zone that failed raises `ReconcileError`, so the job fails."""
        errors = [f"{zone.zone}: {error}" for zone in self.run().zones for error in zone.errors]
        if errors:
            raise ReconcileError("; ".join(errors))

    def submit(self, runner: JobRunner) -> str:
        """Queue one tracked run and return its job id."""
        return runner.submit(KIND, self.run_job).id

    def schedule(self, runner: JobRunner) -> None:
        """Run every applied `cloudflare.reconcile_interval` without filling the job history,
        replacing the earlier schedule. Each successful apply calls it, so a new interval takes
        effect there. Before setup, and with a config that cannot be read, it is the default."""
        seconds = CloudflareSettings().reconcile_seconds
        try:
            if self._store.configured():
                seconds = self._store.load_config().cloudflare.reconcile_seconds
        except (OSError, StoreError) as error:
            logger.warning("cannot read the reconcile interval, so it is %ss: %s", seconds, error)
        runner.schedule_every(KIND, self.run, interval=timedelta(seconds=seconds), track=False)
