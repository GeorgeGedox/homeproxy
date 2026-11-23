"""Cloudflare endpoints: managed records and the pending plan, reconcile, purge and DDNS adoption.

Every call reaches Cloudflare and can wait on its rate limit, so the endpoints are plain `def`
and FastAPI runs them in a worker thread. A zone that fails is reported in its result, and the
other zones still run. A purge and an adoption change Cloudflare outside the apply, so both are
written to the audit log.
"""

from datetime import datetime
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from homeproxy.api.apply import ServicesDep
from homeproxy.api.changes import edit_lock
from homeproxy.auth.errors import AuthError
from homeproxy.auth.routes import Runtime
from homeproxy.cloudflare import CloudflareError, ReconcileError, ZoneResult, purge, reconcile
from homeproxy.cloudflare.ddns import DdnsError, DdnsResult, adopt
from homeproxy.jobs.reconcile import ReconcileRun
from homeproxy.store.pending import PendingChanges

router = APIRouter(prefix="/api/v1", tags=["cloudflare"])

_UPSTREAM: dict[int | str, dict[str, Any]] = {502: {"description": "A Cloudflare call failed"}}


class CloudflareState(BaseModel):
    """What the Cloudflare page shows.

    `enabled` is the integration toggle, pending edits included. `zones` lists, per zone, the
    records HomeProxy owns now, and the changes and conflicts the next apply would bring, from a
    dry run of the pending config. `last_reconcile` is the last run since the API started.
    `ddns` is the last DDNS run: action `hand-made` is the only one `adopt` accepts.
    `ddns_checked_at` is when that run ended, or null before the first one since the API started.
    """

    enabled: bool
    zones: list[ZoneResult]
    last_reconcile: ReconcileRun | None
    ddns: list[DdnsResult]
    ddns_checked_at: datetime | None


@router.get("/cloudflare/records", response_model=CloudflareState)
def get_records(runtime: Runtime, services: ServicesDep) -> CloudflareState:
    """Return the owned records, the pending plan, the last reconcile and the DDNS state."""
    store = runtime.store
    with edit_lock(store) as held:
        overlay = PendingChanges(store).load_overlay(held)
    enabled = overlay.config.cloudflare.enabled
    zones = []
    if enabled:  # secrets.yaml is not read while the integration is off
        zones = reconcile(overlay.config, overlay.routes, store.load_secrets(), dry_run=True)
    return CloudflareState(
        enabled=enabled,
        zones=zones,
        last_reconcile=services.reconcile.last,
        ddns=services.ddns.results,
        ddns_checked_at=services.ddns.checked_at,
    )


@router.post(
    "/cloudflare/reconcile",
    response_model=ReconcileRun,
    responses={409: {"description": "The config cannot load, or has edits that are not applied"}},
)
def run_reconcile(services: ServicesDep) -> ReconcileRun:
    """Make the route records match the applied routes now, then queue a DDNS run. Does nothing
    while the toggle is off."""
    try:
        run = services.reconcile.run()
    except ReconcileError as error:
        raise AuthError(409, "reconcile_refused", str(error)) from None
    services.ddns.submit(services.jobs)
    return run


@router.post("/cloudflare/purge", response_model=list[ZoneResult])
def purge_records(runtime: Runtime) -> list[ZoneResult]:
    """Delete every route record HomeProxy owns, even while the toggle is off.

    Hand-made records and the DDNS record stay.
    """
    store = runtime.store
    zones = purge(store.load_config(), store.load_secrets())
    # A failed write is reported as "<host>: <error>".
    deleted = [
        change.name
        for zone in zones
        for change in zone.changes
        if not any(error.startswith(f"{change.name}: ") for error in zone.errors)
    ]
    errors = [f"{zone.zone}: {error}" for zone in zones for error in zone.errors]
    store.audit.record("admin", "cloudflare.purge", {"deleted": deleted, "errors": errors})
    return zones


@router.post(
    "/cloudflare/ddns/{zone}/adopt",
    response_model=DdnsResult,
    responses=_UPSTREAM | {409: {"description": "No single hand-made A record to adopt"}},
)
def adopt_ddns(zone: str, runtime: Runtime, services: ServicesDep) -> DdnsResult:
    """Take ownership of the hand-made A record on the zone's DDNS name, then run DDNS at once.

    Only the record's comment changes here. The DDNS run moves the address.
    """
    store = runtime.store
    try:
        result = adopt(store.load_config(), store.load_secrets(), zone)
    except DdnsError as error:
        raise AuthError(409, "adopt_refused", str(error)) from None
    except CloudflareError as error:
        raise AuthError(502, "cloudflare_error", str(error)) from None
    if result.action == "adopted":
        store.audit.record("admin", "cloudflare.adopt", {"zone": zone, "record": result.record})
    services.ddns.submit(services.jobs)
    return result
