"""In-memory record of background jobs. Thread-safe.

Jobs live only in the API process. A restart forgets them, which is acceptable because every job
either is rerun by its schedule or is started again by the user.
"""

import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from homeproxy.jobs.models import Job, JobState
from homeproxy.util.redact import redact

# Enough for the error of a failed lego or HTTP call, short enough for an API response.
MAX_ERROR_LENGTH = 2000

_ALLOWED: dict[JobState, frozenset[JobState]] = {
    JobState.QUEUED: frozenset({JobState.RUNNING, JobState.FAILED}),
    JobState.RUNNING: frozenset({JobState.FINISHED, JobState.FAILED}),
    JobState.FINISHED: frozenset(),
    JobState.FAILED: frozenset(),
}


class JobError(Exception):
    """Base class for job tracking errors."""


class UnknownJob(JobError, KeyError):
    """No job has this id, or it was dropped from the history."""

    def __init__(self, job_id: str) -> None:
        super().__init__(f"unknown job {job_id!r}")
        self.job_id = job_id

    def __str__(self) -> str:
        # KeyError would print the repr of the message.
        return str(self.args[0])


class InvalidTransition(JobError):
    """A job cannot move from its current state to the requested one."""

    def __init__(self, job_id: str, current: JobState, requested: JobState) -> None:
        super().__init__(f"job {job_id!r} cannot go from {current} to {requested}")
        self.job_id = job_id
        self.current = current
        self.requested = requested


def _utcnow() -> datetime:
    return datetime.now(UTC)


def error_text(error: BaseException | str) -> str:
    """Return the text kept for a failed job: redacted, never empty, at most `MAX_ERROR_LENGTH`.

    An exception without a message is "failed". The job runner logs its type and traceback.
    """
    text = error if isinstance(error, str) else str(error)
    text = redact(text.strip()) or "failed"
    if len(text) > MAX_ERROR_LENGTH:
        text = text[: MAX_ERROR_LENGTH - 3] + "..."
    return text


class JobTracker:
    """Holds the state of every job that is queued or running, plus the newest final ones.

    `keep` limits how many final jobs stay in the history. Queued and running jobs are never
    dropped.
    """

    def __init__(self, *, keep: int = 200, clock: Callable[[], datetime] = _utcnow) -> None:
        if keep < 1:
            raise ValueError("keep must be at least 1")
        self._keep = keep
        self._clock = clock
        self._lock = threading.Lock()
        # Insertion order is creation order.
        self._jobs: dict[str, Job] = {}

    def create(self, kind: str, subject: str | None = None) -> Job:
        """Record a new job in the `queued` state and return it."""
        job = Job(
            id=uuid.uuid4().hex,
            kind=kind,
            subject=subject,
            state=JobState.QUEUED,
            created_at=self._clock(),
        )
        with self._lock:
            self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> Job:
        """Return a job by id. Raises `UnknownJob`."""
        with self._lock:
            try:
                return self._jobs[job_id]
            except KeyError:
                raise UnknownJob(job_id) from None

    def jobs(self) -> list[Job]:
        """Return every job held, newest first."""
        with self._lock:
            return list(reversed(self._jobs.values()))

    def mark_running(self, job_id: str) -> Job:
        """Move a queued job to `running`."""
        return self._move(job_id, JobState.RUNNING, started_at=self._clock())

    def mark_finished(self, job_id: str) -> Job:
        """Move a running job to `finished`."""
        return self._move(job_id, JobState.FINISHED, finished_at=self._clock())

    def mark_failed(self, job_id: str, error: BaseException | str) -> Job:
        """Move a queued or running job to `failed` and keep its redacted error."""
        return self._move(
            job_id, JobState.FAILED, finished_at=self._clock(), error=error_text(error)
        )

    def fail_if_queued(self, job_id: str, error: str) -> bool:
        """Fail a job only if it has not started, in one step. Returns whether it failed it."""
        try:
            self._move(
                job_id,
                JobState.FAILED,
                expect=JobState.QUEUED,
                finished_at=self._clock(),
                error=error_text(error),
            )
        except JobError:
            return False
        return True

    def _move(
        self, job_id: str, state: JobState, *, expect: JobState | None = None, **changes: object
    ) -> Job:
        with self._lock:
            try:
                job = self._jobs[job_id]
            except KeyError:
                raise UnknownJob(job_id) from None
            if state not in _ALLOWED[job.state] or expect not in (None, job.state):
                raise InvalidTransition(job_id, job.state, state)
            updated = job.model_copy(update={"state": state, **changes})
            self._jobs[job_id] = updated
            if state.is_final:
                self._prune()
            return updated

    def _prune(self) -> None:
        """Drop the oldest final jobs beyond `keep`. The caller holds the lock."""
        final = [job_id for job_id, job in self._jobs.items() if job.state.is_final]
        for job_id in final[: max(0, len(final) - self._keep)]:
            del self._jobs[job_id]
