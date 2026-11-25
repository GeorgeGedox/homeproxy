"""`GET /status`: route and bouncer health, the HAProxy version, uptime and update check, whether
the DNS server is listening and its uptime, the HomeProxy update check, and the last apply.

Health is read from HAProxy's `show stat` on each request, so it is as fresh as HAProxy's own
checks. When HAProxy does not answer, the status still answers, with `haproxy_error` set.
"""

import logging
from collections.abc import Iterable
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from homeproxy.api.apply import ServicesDep
from homeproxy.crowdsec.health import AppsecReading
from homeproxy.dns.probe import dns_listening, dns_uptime_seconds
from homeproxy.dns.snapshot import FILE_NAME as DNS_FILE
from homeproxy.generator import BACKEND_PREFIX
from homeproxy.haproxy.client import HAProxyClient, HAProxyError
from homeproxy.haproxy.release_links import GENERATED
from homeproxy.haproxy.stats import HAProxyInfo, ProxyType, StatRow
from homeproxy.haproxy.version_check import VersionStatus
from homeproxy.jobs.update_check import UpdateCheck, UpdateStatus
from homeproxy.jobs.version_check import VersionCheck
from homeproxy.store import AuditLog, StoreError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["status"])

BOUNCER_BACKEND = "crowdsec-spoa"
"""The backend of the CrowdSec bouncer. A release has it only while CrowdSec is on."""

# Enough entries to find the last apply among other actions, cheap enough to read on each poll.
_AUDIT_SCAN = 1000


class RouteHealth(BaseModel):
    """The health of one live route, from its HAProxy backend. Counters start at each reload."""

    name: str
    status: str = Field(description="HAProxy's backend status, such as `UP` or `DOWN`")
    requests: int | None = Field(description="HTTP requests; None for TCP routes")
    responses_5xx: int | None = Field(description="5xx responses; None for TCP routes")


class LastApply(BaseModel):
    """The result of the newest apply in the audit log."""

    time: datetime
    success: bool
    stage: str
    snapshot_id: str | None
    errors: list[str]
    warnings: list[str] = Field(
        description="The apply's warnings, without upstream health, which `routes` reports live"
    )


class Status(BaseModel):
    """What the Overview page shows. `version_check` and `update_check` are None until their first
    check succeeds."""

    haproxy_version: str | None = Field(description="None when HAProxy did not answer")
    haproxy_error: str | None = Field(description="Why HAProxy could not be read")
    haproxy_uptime_seconds: int | None = Field(
        description="Seconds since HAProxy last started or reloaded. None when it did not answer"
    )
    version_check: VersionStatus | None
    update_check: UpdateStatus | None = Field(description="The last HomeProxy update check")
    update_check_configured: bool = Field(
        description="False while no release source is set. Then nothing is fetched"
    )
    routes: list[RouteHealth]
    bouncer: Literal["up", "down"] | None = Field(
        description="The CrowdSec bouncer as HAProxy's health check sees it. None when CrowdSec "
        "is off in the running release, or HAProxy did not answer"
    )
    appsec: Literal["up", "down"] | None = Field(
        description="Whether AppSec checks a request for the bouncer, with the saved settings. "
        "None when CrowdSec or AppSec is off"
    )
    dns_listening: bool = Field(
        description="Whether the DNS server accepts connections on the address of the live release"
    )
    dns_uptime_seconds: int | None = Field(
        description="Seconds since the DNS server's process started. None when it is not "
        "listening or its start time is unknown"
    )
    last_apply: LastApply | None


def bouncer_state(rows: Iterable[StatRow]) -> Literal["up", "down"] | None:
    """Return the bouncer's state from `show stat`, or None when HAProxy has no bouncer backend."""
    for row in rows:
        if row.type is ProxyType.BACKEND and row.proxy == BOUNCER_BACKEND:
            return "up" if row.status.startswith("UP") else "down"
    return None


def _haproxy(
    client: HAProxyClient,
) -> tuple[HAProxyInfo, list[RouteHealth], Literal["up", "down"] | None]:
    """Return `show info`, the health of each route and the bouncer's state. Raises
    `HAProxyError`."""
    info = client.show_info()
    rows = client.show_stat()
    routes = [
        RouteHealth(
            name=row.proxy.removeprefix(BACKEND_PREFIX),
            status=row.status,
            requests=row.requests_total,
            responses_5xx=row.responses_5xx,
        )
        for row in rows
        if row.type is ProxyType.BACKEND and row.proxy.startswith(BACKEND_PREFIX)
    ]
    return info, routes, bouncer_state(rows)


def _last_apply(audit: AuditLog) -> LastApply | None:
    entry = next((e for e in audit.read(_AUDIT_SCAN) if e.action == "apply"), None)
    if entry is None:
        return None
    details = entry.details
    return LastApply.model_validate(
        {
            "time": entry.time,
            "success": details.get("success"),
            "stage": details.get("stage"),
            "snapshot_id": details.get("snapshot"),
            "errors": details.get("errors", []),
            # Older entries have no warnings.
            "warnings": details.get("warnings", []),
        }
    )


@router.get("/status", response_model=Status)
def status(request: Request, services: ServicesDep) -> Status:
    """Return route and bouncer health, the HAProxy version, uptime and update check, whether the
    DNS server is listening and its uptime, the HomeProxy update check, and the last apply."""
    check: VersionCheck = request.app.state.version_check
    update: UpdateCheck = request.app.state.update_check
    reading: AppsecReading = request.app.state.appsec
    info: HAProxyInfo | None = None
    error: str | None = None
    routes: list[RouteHealth] = []
    bouncer: Literal["up", "down"] | None = None
    try:
        info, routes, bouncer = _haproxy(services.pipeline.client)
    except HAProxyError as exc:
        error = str(exc)
    store = services.pipeline.store
    try:
        appsec = reading()
    except (OSError, StoreError) as exc:
        logger.warning("cannot read the CrowdSec settings for the AppSec state: %s", exc)
        appsec = None
    listening = dns_listening(store.root / GENERATED / DNS_FILE)
    dns_uptime = dns_uptime_seconds(services.pipeline.dns_pid_file) if listening else None
    return Status(
        haproxy_version=info.version if info else None,
        haproxy_error=error,
        haproxy_uptime_seconds=info.uptime_seconds if info else None,
        version_check=check.status,
        update_check=update.status,
        update_check_configured=update.url is not None,
        routes=routes,
        bouncer=bouncer,
        appsec=appsec,
        dns_listening=listening,
        dns_uptime_seconds=dns_uptime,
        last_apply=_last_apply(store.audit),
    )
