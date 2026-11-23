"""Runs background jobs on an in-process APScheduler and records their state in a `JobTracker`.

Job functions are plain synchronous callables. They run in a thread pool, so they may block on a
subprocess or a network call, and they must not touch the event loop. An exception from a job
fails that job only: the scheduler, the API and HAProxy keep running.
"""

import logging
import re
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from apscheduler.events import EVENT_JOB_MAX_INSTANCES, EVENT_JOB_MISSED
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from homeproxy.jobs.models import KIND_PATTERN, Job, JobState
from homeproxy.jobs.tracker import JobError, JobTracker

log = logging.getLogger(__name__)

JobFunction = Callable[[], object]

DEFAULT_WORKERS = 4

SERIAL = "serial"
"""The executor with one worker, for jobs that would only wait on one lock in turn. Waiting there,
they leave the other workers to the rest, such as the heartbeat."""


class RunnerStopped(JobError):
    """The runner was shut down, so it accepts no more jobs."""

    def __init__(self) -> None:
        super().__init__("the job runner is stopped")


class JobRunner:
    """Owns the scheduler. Create one per API process, `start` it at startup, `shutdown` at exit.

    `submit` runs a function once, as soon as a worker is free, and returns the queued job.
    `schedule_every` runs a function on an interval. Jobs submitted before `start` wait in the
    `queued` state until it is called.
    """

    def __init__(self, *, max_workers: int = DEFAULT_WORKERS) -> None:
        self.tracker = JobTracker()
        self._lock = threading.Lock()
        self._stopped = False
        self._scheduler: Any = BackgroundScheduler(
            timezone=UTC,
            executors={
                "default": ThreadPoolExecutor(max_workers=max_workers),
                SERIAL: ThreadPoolExecutor(max_workers=1),
            },
            job_defaults={
                # Run late rather than never: a dropped one-off run would stay queued forever.
                "misfire_grace_time": None,
                # A recurring job that fell behind runs once, not once per missed interval.
                "coalesce": True,
                "max_instances": 1,
            },
        )
        self._scheduler.add_listener(self._on_not_run, EVENT_JOB_MISSED | EVENT_JOB_MAX_INSTANCES)

    def start(self) -> None:
        """Start the scheduler thread. Raises `RunnerStopped` after `shutdown`."""
        with self._lock:
            if self._stopped:
                raise RunnerStopped
            if not self._scheduler.running:
                self._scheduler.start()

    def shutdown(self, *, wait: bool = False) -> None:
        """Stop the scheduler and fail every job that has not started.

        Running jobs are not interrupted. With `wait`, this returns after they end.
        """
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            if self._scheduler.running:
                # This waits for the scheduler thread to finish handing out due jobs. A shutdown
                # in between made APScheduler 3.11 raise `JobLookupError` in that thread.
                self._scheduler.remove_all_jobs()
                self._scheduler.shutdown(wait=wait)
        for job in self.tracker.jobs():
            if job.state is JobState.QUEUED:
                self.tracker.fail_if_queued(job.id, "the dashboard stopped before the job ran")

    def submit(
        self, kind: str, func: JobFunction, *, subject: str | None = None, serial: bool = False
    ) -> Job:
        """Queue `func` to run once and return its job in the `queued` state. With `serial`, it
        runs on the `SERIAL` executor.

        Raises `RunnerStopped` after `shutdown`.
        """
        with self._lock:
            if self._stopped:
                raise RunnerStopped
            job = self.tracker.create(kind, subject)
            self._scheduler.add_job(
                self._run_tracked,
                args=(job.id, func),
                id=job.id,
                name=kind,
                executor=SERIAL if serial else "default",
            )
        log.info("job %s queued: %s", job.id, kind)
        return job

    def schedule_every(
        self,
        kind: str,
        func: JobFunction,
        *,
        interval: timedelta,
        first_run: datetime | None = None,
        jitter: timedelta | None = None,
        track: bool = True,
        serial: bool = False,
    ) -> None:
        """Run `func` every `interval`, replacing an earlier schedule of the same `kind`.

        The first run is at `first_run`, or one interval after `start`. `jitter` delays each run
        by up to that much. Runs of one kind never overlap. With `track`, each run is recorded as
        a job of that kind; leave it off for frequent work that would push other jobs out of the
        history. A failed run is logged either way, and the next run still happens. With `serial`,
        it runs on the `SERIAL` executor.
        """
        if not re.fullmatch(KIND_PATTERN, kind):
            raise ValueError("invalid job kind")
        if interval <= timedelta(0):
            raise ValueError("interval must be positive")
        trigger = IntervalTrigger(
            seconds=interval.total_seconds(),
            start_date=first_run,
            timezone=UTC,
            jitter=None if jitter is None else jitter.total_seconds(),
        )
        with self._lock:
            if self._stopped:
                raise RunnerStopped
            self._scheduler.add_job(
                self._run_scheduled,
                trigger=trigger,
                args=(kind, func, track),
                id=f"schedule:{kind}",
                name=kind,
                replace_existing=True,
                executor=SERIAL if serial else "default",
            )

    def _run_scheduled(self, kind: str, func: JobFunction, track: bool) -> None:
        if track:
            self._run_tracked(self.tracker.create(kind).id, func)
            return
        try:
            func()
        except Exception:
            log.exception("scheduled %s failed", kind)

    def _run_tracked(self, job_id: str, func: JobFunction) -> None:
        """Run one job and record each state. Never raises an `Exception`."""
        try:
            self.tracker.mark_running(job_id)
        except JobError:
            # Shutdown failed it while it waited for a worker.
            return
        log.info("job %s running", job_id)
        try:
            func()
        except BaseException as error:
            log.exception("job %s failed", job_id)
            self.tracker.mark_failed(job_id, error)
            if not isinstance(error, Exception):
                raise
            return
        self.tracker.mark_finished(job_id)
        log.info("job %s finished", job_id)

    def _on_not_run(self, event: Any) -> None:
        """Fail a one-off job that the scheduler skipped, so it does not stay queued forever.

        A skipped run of a schedule has an id the tracker does not know, so it is ignored.
        """
        self.tracker.fail_if_queued(str(event.job_id), "the scheduler skipped the job")
