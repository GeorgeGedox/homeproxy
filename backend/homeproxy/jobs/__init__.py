"""Background jobs: an in-process APScheduler and the state of every job it runs.

Work that can outlast a request, such as certificate issuance or a GeoIP refresh, goes through
`JobRunner.submit`, and the endpoint returns `202` with the job id. `GET /jobs` reads
`JobRunner.tracker`. Recurring work, such as the daily renewal check, uses
`JobRunner.schedule_every`.

A job moves from `queued` to `running` to `finished`, or to `failed` with its redacted error.
"""

from homeproxy.jobs.models import KIND_PATTERN, Job, JobState
from homeproxy.jobs.runner import DEFAULT_WORKERS, JobFunction, JobRunner, RunnerStopped
from homeproxy.jobs.tracker import (
    MAX_ERROR_LENGTH,
    InvalidTransition,
    JobError,
    JobTracker,
    UnknownJob,
    error_text,
)

__all__ = [
    "DEFAULT_WORKERS",
    "KIND_PATTERN",
    "MAX_ERROR_LENGTH",
    "InvalidTransition",
    "Job",
    "JobError",
    "JobFunction",
    "JobRunner",
    "JobState",
    "JobTracker",
    "RunnerStopped",
    "UnknownJob",
    "error_text",
]
