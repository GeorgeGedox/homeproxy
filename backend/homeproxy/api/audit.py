"""`GET /audit`: the audit log, newest first. Secrets are redacted when written and when read."""

from typing import Annotated

from fastapi import APIRouter, Query

from homeproxy.auth.routes import Runtime
from homeproxy.store import AuditEntry

router = APIRouter(prefix="/api/v1", tags=["audit"])


@router.get("/audit", response_model=list[AuditEntry])
def read_audit(
    runtime: Runtime, limit: Annotated[int, Query(ge=1, le=1000)] = 100
) -> list[AuditEntry]:
    """Return up to `limit` entries, newest first: who did what, and when."""
    return runtime.store.audit.read(limit)
