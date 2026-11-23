"""The tracker moves jobs through their states, keeps errors redacted and bounds its history."""

import secrets
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from homeproxy.jobs import (
    MAX_ERROR_LENGTH,
    InvalidTransition,
    JobState,
    JobTracker,
    UnknownJob,
    error_text,
)
from homeproxy.util.redact import REDACTED, register_secret


class FakeClock:
    """A clock that moves one second on every read."""

    def __init__(self) -> None:
        self.now = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


def test_job_goes_queued_running_finished_with_times() -> None:
    tracker = JobTracker(clock=FakeClock())
    job = tracker.create("certificate.issue", subject="example.com")
    assert job.state is JobState.QUEUED
    assert (job.started_at, job.finished_at, job.error) == (None, None, None)

    running = tracker.mark_running(job.id)
    assert running.state is JobState.RUNNING
    assert running.started_at is not None
    assert running.started_at > job.created_at

    finished = tracker.mark_finished(job.id)
    assert finished.state is JobState.FINISHED
    assert finished.finished_at is not None
    assert finished.finished_at > running.started_at
    assert finished.error is None
    assert tracker.get(job.id) == finished
    assert finished.subject == "example.com"


def test_failed_job_keeps_its_error() -> None:
    tracker = JobTracker()
    job = tracker.create("geoip.refresh")
    tracker.mark_running(job.id)
    failed = tracker.mark_failed(job.id, RuntimeError("download returned 503"))
    assert failed.state is JobState.FAILED
    assert failed.error == "download returned 503"
    assert failed.finished_at is not None
    assert tracker.get(job.id).error == "download returned 503"


def test_queued_job_can_fail_without_running() -> None:
    tracker = JobTracker()
    job = tracker.create("geoip.refresh")
    failed = tracker.mark_failed(job.id, "stopped")
    assert (failed.state, failed.started_at, failed.error) == (JobState.FAILED, None, "stopped")


@pytest.mark.parametrize(
    ("steps", "bad"),
    [
        ([], JobState.FINISHED),
        (["running", "finished"], JobState.RUNNING),
        (["running", "finished"], JobState.FAILED),
        (["failed"], JobState.FAILED),
        (["running"], JobState.RUNNING),
    ],
)
def test_invalid_transitions_raise_and_change_nothing(steps: list[str], bad: JobState) -> None:
    tracker = JobTracker()
    job_id = tracker.create("test.job").id
    for step in steps:
        if step == "running":
            tracker.mark_running(job_id)
        elif step == "finished":
            tracker.mark_finished(job_id)
        else:
            tracker.mark_failed(job_id, "boom")
    before = tracker.get(job_id)
    with pytest.raises(InvalidTransition):
        if bad is JobState.RUNNING:
            tracker.mark_running(job_id)
        elif bad is JobState.FINISHED:
            tracker.mark_finished(job_id)
        else:
            tracker.mark_failed(job_id, "again")
    assert tracker.get(job_id) == before


def test_unknown_job_raises() -> None:
    tracker = JobTracker()
    with pytest.raises(UnknownJob, match="unknown job 'nope'"):
        tracker.get("nope")
    with pytest.raises(KeyError):
        tracker.mark_running("nope")


def test_fail_if_queued_only_fails_unstarted_jobs() -> None:
    tracker = JobTracker()
    queued = tracker.create("test.job").id
    running = tracker.create("test.job").id
    tracker.mark_running(running)
    assert tracker.fail_if_queued(queued, "stopped") is True
    assert tracker.fail_if_queued(running, "stopped") is False
    assert tracker.fail_if_queued("missing", "stopped") is False
    assert tracker.get(queued).state is JobState.FAILED
    assert tracker.get(running).state is JobState.RUNNING


def test_jobs_are_listed_newest_first() -> None:
    tracker = JobTracker()
    ids = [tracker.create("test.job").id for _ in range(3)]
    assert [job.id for job in tracker.jobs()] == ids[::-1]


def test_history_drops_oldest_final_jobs_but_never_active_ones() -> None:
    tracker = JobTracker(keep=2)
    active = tracker.create("test.job").id
    running = tracker.create("test.job").id
    tracker.mark_running(running)
    done = []
    for _ in range(4):
        job_id = tracker.create("test.job").id
        tracker.mark_failed(job_id, "boom")
        done.append(job_id)
    held = {job.id for job in tracker.jobs()}
    assert held == {active, running, done[2], done[3]}
    with pytest.raises(UnknownJob):
        tracker.get(done[0])


def test_keep_must_be_positive() -> None:
    with pytest.raises(ValueError, match="keep"):
        JobTracker(keep=0)


@pytest.mark.parametrize("kind", ["", "Cert", "a b", "x\nfrontend evil", "a" * 64, "-a"])
def test_invalid_kind_is_rejected(kind: str) -> None:
    with pytest.raises(ValidationError):
        JobTracker().create(kind)


def test_error_text_redacts_registered_secrets() -> None:
    token = secrets.token_urlsafe(24)
    register_secret(token)
    tracker = JobTracker()
    job_id = tracker.create("certificate.issue").id
    tracker.mark_running(job_id)
    failed = tracker.mark_failed(job_id, PermissionError(f"token {token} lacks Zone:Read"))
    assert failed.error == f"token {REDACTED} lacks Zone:Read"
    assert token not in failed.model_dump_json()


def test_error_text_is_never_empty() -> None:
    assert error_text(TimeoutError()) == "failed"
    assert error_text("   ") == "failed"


def test_error_text_is_bounded() -> None:
    text = error_text("x" * (MAX_ERROR_LENGTH * 2))
    assert len(text) == MAX_ERROR_LENGTH
    assert text.endswith("...")


def test_job_serializes_to_the_api_shape() -> None:
    tracker = JobTracker()
    job_id = tracker.create("geoip.refresh").id
    data = tracker.mark_failed(job_id, "boom").model_dump(mode="json")
    assert data["state"] == "failed"
    assert data["error"] == "boom"
    assert set(data) == {
        "id",
        "kind",
        "subject",
        "state",
        "created_at",
        "started_at",
        "finished_at",
        "error",
    }
