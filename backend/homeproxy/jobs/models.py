"""The state of one background job, as `GET /jobs` and `GET /jobs/{id}` return it."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

# A kind names the work, such as `certificate.issue` or `geoip.refresh`. Code sets it, never a user.
KIND_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,62}$"


class JobState(StrEnum):
    """Where a job is in its life. `finished` and `failed` are final."""

    QUEUED = "queued"
    RUNNING = "running"
    FINISHED = "finished"
    FAILED = "failed"

    @property
    def is_final(self) -> bool:
        """True when the job will not change again."""
        return self in (JobState.FINISHED, JobState.FAILED)


class Job(BaseModel):
    """A snapshot of one background job. The tracker returns a new snapshot on every change.

    `error` is set only when `state` is `failed`. Its text has every registered secret redacted.
    Times are timezone-aware UTC.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(
        description="Opaque job id, returned with `202` by the endpoint that started it"
    )
    kind: str = Field(pattern=KIND_PATTERN, description="What the job does, e.g. `geoip.refresh`")
    subject: str | None = Field(
        default=None, description="What the job works on, e.g. a zone, when the kind has one"
    )
    state: JobState
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = Field(default=None, description="Why a failed job failed, redacted")
