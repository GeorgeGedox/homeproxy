"""Jobs run on the real scheduler and report queued, running, finished and failed states."""

import threading
import time
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any

import pytest
from apscheduler.executors.base import BaseExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.base import STATE_STOPPED
from apscheduler.triggers.date import DateTrigger

from homeproxy.jobs import Job, JobRunner, JobState, RunnerStopped
from tests.conftest import wait_until

DEADLINE = 10.0
HOLD = 0.2


def wait_state(runner: JobRunner, job: Job, state: JobState) -> Job:
    wait_until(
        lambda: runner.tracker.get(job.id).state is state,
        f"job {job.id} to be {state.value}",
        timeout=DEADLINE,
        interval=0.01,
    )
    return runner.tracker.get(job.id)


class Gate:
    """A job function that signals when it starts and blocks until released."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def __call__(self) -> None:
        self.started.set()
        if not self.release.wait(DEADLINE):
            raise TimeoutError("gate never released")


@pytest.fixture
def gates() -> Iterator[list[Gate]]:
    made: list[Gate] = []
    yield made
    for gate in made:
        gate.release.set()


@pytest.fixture
def runner(gates: list[Gate]) -> Iterator[JobRunner]:
    runner = JobRunner(max_workers=1)
    yield runner
    for gate in gates:
        gate.release.set()
    runner.shutdown(wait=True)


def new_gate(gates: list[Gate]) -> Gate:
    gate = Gate()
    gates.append(gate)
    return gate


def test_job_reports_queued_running_and_finished(runner: JobRunner, gates: list[Gate]) -> None:
    runner.start()
    gate = new_gate(gates)
    job = runner.submit("test.job", gate)
    assert job.state is JobState.QUEUED

    assert gate.started.wait(DEADLINE)
    wait_state(runner, job, JobState.RUNNING)
    gate.release.set()
    assert wait_state(runner, job, JobState.FINISHED).error is None


def test_runner_goes_on_after_a_failed_job(runner: JobRunner) -> None:
    def broken() -> None:
        raise RuntimeError("lego exited with status 1: rate limited")

    runner.start()
    job = runner.submit("certificate.issue", broken)
    wait_state(runner, job, JobState.FAILED)

    after = runner.submit("test.job", lambda: None)
    wait_state(runner, after, JobState.FINISHED)


def test_job_submitted_before_start_waits_queued(runner: JobRunner) -> None:
    ran = threading.Event()
    before = set(threading.enumerate())
    job = runner.submit("test.job", ran.set)
    # No thread started, so nothing can run the job before `start`.
    assert set(threading.enumerate()) <= before
    assert runner.tracker.get(job.id).state is JobState.QUEUED
    assert not ran.is_set()
    runner.start()
    wait_state(runner, job, JobState.FINISHED)
    assert ran.is_set()


def test_shutdown_fails_queued_jobs_and_lets_running_ones_end(
    runner: JobRunner, gates: list[Gate], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Nothing in the runner shows the hand-off to the pool, where the job waits for the busy
    # worker, so the executor records it.
    handed: set[str] = set()
    submit_job = BaseExecutor.submit_job

    def record_hand_off(self: BaseExecutor, job: Any, run_times: list[datetime]) -> None:
        submit_job(self, job, run_times)
        handed.add(job.id)

    monkeypatch.setattr(BaseExecutor, "submit_job", record_hand_off)
    before = set(threading.enumerate())
    runner.start()
    gate = new_gate(gates)
    running = runner.submit("test.blocker", gate)
    assert gate.started.wait(DEADLINE)
    never = threading.Event()
    queued = runner.submit("test.job", never.set)
    wait_until(
        lambda: queued.id in handed,
        "the scheduler to hand the job to the pool",
        timeout=DEADLINE,
        interval=0.01,
    )

    runner.shutdown(wait=False)
    stopped = runner.tracker.get(queued.id)
    assert stopped.state is JobState.FAILED
    assert stopped.error == "the dashboard stopped before the job ran"

    gate.release.set()
    wait_state(runner, running, JobState.FINISHED)
    # A stopped pool's workers end only after the jobs already handed to them.
    for thread in set(threading.enumerate()) - before:
        thread.join(DEADLINE)
        assert not thread.is_alive()
    assert not never.is_set()
    assert runner.tracker.get(queued.id).state is JobState.FAILED


def stopped(stopping: list[BackgroundScheduler]) -> bool:
    return bool(stopping) and stopping[0].state == STATE_STOPPED


def test_shutdown_right_after_a_job_ends_leaves_the_scheduler_thread_quiet(
    runner: JobRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Seen once in a test run. The scheduler thread removes a finished one-off
    job after handing it to a worker. A shutdown in between made APScheduler 3.11 raise
    `JobLookupError` in that thread. The trigger holds the thread there until the scheduler
    stops. Only the scheduler's own state shows that moment, so the test reads it; a shutdown
    that waits for the thread never lets it stop, so the hold ends after `HOLD` seconds."""
    raised: list[BaseException | None] = []
    monkeypatch.setattr(threading, "excepthook", lambda args: raised.append(args.exc_value))
    stopping: list[BackgroundScheduler] = []
    shutdown = BackgroundScheduler.shutdown

    def record_shutdown(self: BackgroundScheduler, *args: Any, **kwargs: Any) -> None:
        stopping.append(self)
        shutdown(self, *args, **kwargs)

    monkeypatch.setattr(BackgroundScheduler, "shutdown", record_shutdown)
    next_fire_time = DateTrigger.get_next_fire_time
    shutting_down = threading.Event()
    runs_seen: list[datetime] = []

    def after_shutdown(self: DateTrigger, previous: datetime, now: datetime) -> datetime | None:
        # APScheduler asks with the run time twice: to list the due runs, then after it handed
        # them to a worker.
        if previous is not None:
            runs_seen.append(previous)
        if len(runs_seen) == 2 and shutting_down.wait(DEADLINE):
            deadline = time.monotonic() + HOLD
            while not stopped(stopping) and time.monotonic() < deadline:
                time.sleep(0.01)
        fire_time: datetime | None = next_fire_time(self, previous, now)
        return fire_time

    monkeypatch.setattr(DateTrigger, "get_next_fire_time", after_shutdown)
    job = runner.submit("test.job", lambda: None)
    runner.start()
    wait_state(runner, job, JobState.FINISHED)

    shutting_down.set()
    runner.shutdown(wait=True)

    assert raised == []


def test_stopped_runner_refuses_work(runner: JobRunner) -> None:
    runner.start()
    runner.shutdown()
    with pytest.raises(RunnerStopped):
        runner.submit("test.job", lambda: None)
    with pytest.raises(RunnerStopped):
        runner.start()
    with pytest.raises(RunnerStopped):
        runner.schedule_every("test.job", lambda: None, interval=timedelta(seconds=1))


def test_schedule_every_records_each_run(runner: JobRunner) -> None:
    calls: list[int] = []
    runner.schedule_every(
        "test.recurring", lambda: calls.append(1), interval=timedelta(milliseconds=50)
    )
    runner.start()
    wait_until(lambda: len(calls) >= 2, "two runs", timeout=DEADLINE, interval=0.01)
    wait_until(
        lambda: sum(job.state is JobState.FINISHED for job in runner.tracker.jobs()) >= 2,
        "two finished runs",
        timeout=DEADLINE,
        interval=0.01,
    )
    assert {job.kind for job in runner.tracker.jobs()} == {"test.recurring"}


def test_failed_scheduled_run_does_not_stop_the_schedule(runner: JobRunner) -> None:
    calls: list[int] = []

    def broken() -> None:
        calls.append(1)
        raise ValueError(f"attempt {len(calls)} failed")

    runner.schedule_every("test.recurring", broken, interval=timedelta(milliseconds=50))
    runner.start()
    wait_until(lambda: len(calls) >= 3, "three runs", timeout=DEADLINE, interval=0.01)
    wait_until(
        lambda: sum(job.state is JobState.FAILED for job in runner.tracker.jobs()) >= 2,
        "two failed runs",
        timeout=DEADLINE,
        interval=0.01,
    )
    errors = {job.error for job in runner.tracker.jobs() if job.state is JobState.FAILED}
    assert "attempt 1 failed" in errors


def test_untracked_schedule_runs_without_history(runner: JobRunner) -> None:
    calls: list[int] = []

    def flaky() -> None:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("first run fails")

    runner.schedule_every("ddns.update", flaky, interval=timedelta(milliseconds=50), track=False)
    runner.start()
    wait_until(lambda: len(calls) >= 3, "three runs", timeout=DEADLINE, interval=0.01)
    assert runner.tracker.jobs() == []


def test_schedule_of_the_same_kind_replaces_the_earlier_one(runner: JobRunner) -> None:
    old: list[int] = []
    new: list[int] = []
    runner.schedule_every("test.recurring", lambda: old.append(1), interval=timedelta(hours=1))
    runner.schedule_every(
        "test.recurring", lambda: new.append(1), interval=timedelta(milliseconds=50)
    )
    runner.start()
    wait_until(
        lambda: len(new) >= 2, "two runs of the new schedule", timeout=DEADLINE, interval=0.01
    )
    assert old == []


@pytest.mark.parametrize(
    ("kind", "interval"),
    [("Bad Kind", timedelta(seconds=1)), ("test.job", timedelta(0))],
)
def test_schedule_every_rejects_bad_arguments(
    runner: JobRunner, kind: str, interval: timedelta
) -> None:
    with pytest.raises(ValueError, match=r"kind|interval"):
        runner.schedule_every(kind, lambda: None, interval=interval)
