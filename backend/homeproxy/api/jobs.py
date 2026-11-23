"""`GET /jobs` and `GET /jobs/{id}`: the state of background jobs, held in memory only."""

from fastapi import APIRouter

from homeproxy.api.apply import ServicesDep
from homeproxy.auth.errors import AuthError
from homeproxy.jobs import Job, UnknownJob

router = APIRouter(prefix="/api/v1", tags=["jobs"])


@router.get("/jobs", response_model=list[Job])
def list_jobs(services: ServicesDep) -> list[Job]:
    """Return the recent jobs, newest first. A restart empties the list."""
    return services.jobs.tracker.jobs()


@router.get("/jobs/{job_id}", response_model=Job, responses={404: {"description": "No such job"}})
def get_job(job_id: str, services: ServicesDep) -> Job:
    """Return one job, for example one whose id an apply or a renewal returned."""
    try:
        return services.jobs.tracker.get(job_id)
    except UnknownJob:
        raise AuthError(404, "job_not_found", "No job has this id.") from None
