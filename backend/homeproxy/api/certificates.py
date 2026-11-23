"""`GET /certificates`, `GET /certificates/planned` and `POST /certificates/{name}/renew`.

A renewal is a forced issuance that runs as a background job. Its id comes back with `202`.
"""

from fastapi import APIRouter
from pydantic import BaseModel

from homeproxy.api.apply import ServicesDep
from homeproxy.api.changes import edit_lock
from homeproxy.auth.errors import AuthError
from homeproxy.auth.routes import Runtime
from homeproxy.certs.bundle import CertificateInfo
from homeproxy.certs.plan import plan_certificates
from homeproxy.jobs import Job
from homeproxy.jobs.certs import submit_issue
from homeproxy.store.pending import PendingChanges

router = APIRouter(prefix="/api/v1", tags=["certificates"])


@router.get("/certificates", response_model=list[CertificateInfo])
def list_certificates(services: ServicesDep) -> list[CertificateInfo]:
    """Return the installed certificates with their names, issuer and expiry."""
    return services.certificates.certificates()


class PlannedCertificate(BaseModel):
    """A certificate the staged config needs, and the domain it belongs to."""

    name: str
    zone: str


@router.get("/certificates/planned", response_model=list[PlannedCertificate])
def planned_certificates(runtime: Runtime) -> list[PlannedCertificate]:
    """Return the certificates the next apply plans, pending edits included, sorted by name."""
    with edit_lock(runtime.store) as held:
        overlay = PendingChanges(runtime.store).load_overlay(held)
    return [
        PlannedCertificate(name=spec.name, zone=spec.zone)
        for spec in plan_certificates(overlay.config, overlay.routes)
    ]


@router.post(
    "/certificates/{name}/renew",
    status_code=202,
    response_model=Job,
    responses={404: {"description": "The live config needs no certificate by this name"}},
)
def renew_certificate(name: str, services: ServicesDep) -> Job:
    """Start renewing one certificate now, even when it is not due, and return its job."""
    if name not in {spec.name for spec in services.certificates.plan()}:
        raise AuthError(
            404, "certificate_not_found", "The config needs no certificate by this name."
        )
    return submit_issue(services.jobs, services.certificates, name, force=True)
