"""Security endpoints: CrowdSec decisions through the LAPI, and the HAProxy-side allowlist.

The LAPI calls block, so the endpoints are plain `def` and FastAPI runs them in a worker thread.
Decisions use the dashboard's machine credentials from `secrets.yaml`, which no answer holds.
Allowlist edits need no apply: they write `config.yaml`, HAProxy and the live release at once, and
are refused while other changes are pending. Every change is written to the audit log.
"""

import logging
from collections.abc import Callable
from datetime import timedelta

from fastapi import APIRouter, Response
from pydantic import BaseModel, Field

from homeproxy.api.apply import ServicesDep
from homeproxy.api.errors import ApiError, changes_pending
from homeproxy.auth.routes import Runtime
from homeproxy.crowdsec import (
    ChangesPending,
    Decision,
    DecisionType,
    LapiClient,
    LapiError,
    add_to_allowlist,
    remove_from_allowlist,
)
from homeproxy.haproxy.apply_rules import public_ranges
from homeproxy.haproxy.client import HAProxyClient, HAProxyError
from homeproxy.models import StrictModel
from homeproxy.models.fields import IpOrCidr
from homeproxy.store import Store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["security"])

MAX_DECISION_SECONDS = 10 * 365 * 24 * 3600
"""Far longer than any ban needs, and well inside the LAPI's duration range."""

BLOCKLIST_ORIGINS = frozenset({"CAPI", "lists"})
"""Origins of blocklist decisions, which can number tens of thousands."""

_LAPI_RESPONSES: dict[int | str, dict[str, str]] = {
    409: {"description": "CrowdSec is off, or the machine credentials are missing"},
    502: {"description": "The LAPI could not be reached or refused the call"},
}
_ALLOWLIST_RESPONSES: dict[int | str, dict[str, str]] = {
    409: {"description": "Changes are pending; apply or discard them first"},
    502: {"description": "The change is saved, but HAProxy could not be updated; send it again"},
}


class NewDecision(StrictModel):
    """A manual ban or captcha for an address or a range."""

    value: IpOrCidr
    type: DecisionType
    duration_seconds: int = Field(ge=1, le=MAX_DECISION_SECONDS)


class AllowlistEntry(StrictModel):
    """An address or a range for the allowlist."""

    entry: IpOrCidr


class DecisionCounts(BaseModel):
    """The active decisions of the local engine and of manual bans, and whether a blocklist is
    loaded too."""

    bans: int
    captchas: int
    blocklist: bool


class AllowlistAdded(BaseModel):
    """The allowlist after an addition, and a warning when the new entry is a public range."""

    allowlist: list[str]
    warnings: list[str]


def _lapi(store: Store) -> LapiClient:
    """Return a client for the configured LAPI, as the dashboard's machine."""
    crowdsec = store.load_config().crowdsec
    machine = store.load_secrets().crowdsec
    machine_id = machine.machine_id.get_secret_value()
    password = machine.machine_password.get_secret_value()
    if not crowdsec.enabled or crowdsec.lapi_url is None or not machine_id or not password:
        raise ApiError(
            409,
            "crowdsec_not_configured",
            "Turn CrowdSec on in Settings, set its API URL, machine ID and machine password there, "
            "then apply.",
        )
    return LapiClient(crowdsec.lapi_url, machine_id, password)


def _lapi_failed(error: LapiError) -> ApiError:
    return ApiError(502, "crowdsec_error", str(error))


@router.get("/security/decisions", response_model=list[Decision], responses=_LAPI_RESPONSES)
def list_decisions(runtime: Runtime) -> list[Decision]:
    """Return every active ban and captcha, blocklists included, newest first."""
    try:
        return _lapi(runtime.store).decisions()
    except LapiError as error:
        raise _lapi_failed(error) from None


@router.get("/security/decisions/counts", response_model=DecisionCounts, responses=_LAPI_RESPONSES)
def count_decisions(runtime: Runtime) -> DecisionCounts:
    """Count the decisions without sending them, for the Overview's status strip."""
    try:
        decisions = _lapi(runtime.store).decisions()
    except LapiError as error:
        raise _lapi_failed(error) from None
    local = [d for d in decisions if d.origin not in BLOCKLIST_ORIGINS]
    bans = sum(d.type == "ban" for d in local)
    return DecisionCounts(
        bans=bans, captchas=len(local) - bans, blocklist=len(local) < len(decisions)
    )


@router.post(
    "/security/decisions",
    status_code=204,
    response_class=Response,
    responses=_LAPI_RESPONSES | {422: {"description": "The value or duration is not valid"}},
)
def add_decision(body: NewDecision, runtime: Runtime) -> Response:
    """Ban or captcha an address or a range. To extend a decision, add a longer one: CrowdSec
    enforces the longest."""
    store = runtime.store
    client = _lapi(store)
    try:
        client.add_decision(body.value, body.type, timedelta(seconds=body.duration_seconds))
    except ValueError as error:
        raise ApiError(422, "validation_failed", str(error), field="value") from None
    except LapiError as error:
        raise _lapi_failed(error) from None
    store.audit.record("admin", "security.decision.add", body.model_dump())
    return Response(status_code=204)


@router.delete(
    "/security/decisions/{decision_id}",
    status_code=204,
    response_class=Response,
    responses=_LAPI_RESPONSES,
)
def delete_decision(decision_id: int, runtime: Runtime) -> Response:
    """Delete one decision. The LAPI answers an unknown id as an error, so it is a 502.

    A blocklist can bring the value back at its next pull. The allowlist keeps it out.
    """
    store = runtime.store
    client = _lapi(store)
    try:
        # Looked up first, so the audit log can say which address was let in. Only for that, so
        # a listing that fails, as one of every blocklist decision can, does not stop the unban.
        decision = next((d for d in client.decisions() if d.id == decision_id), None)
    except LapiError as error:
        logger.warning("decision %s not looked up for the audit log: %s", decision_id, error)
        decision = None
    try:
        client.delete_decision(decision_id)
    except LapiError as error:
        raise _lapi_failed(error) from None
    details: dict[str, object] = {"id": decision_id}
    if decision is not None:
        details |= {"value": decision.value, "type": decision.type, "origin": decision.origin}
    store.audit.record("admin", "security.decision.delete", details)
    return Response(status_code=204)


@router.get("/security/allowlist", response_model=list[str])
def get_allowlist(runtime: Runtime) -> list[str]:
    """Return the allowlist. It is live, so pending changes never hold a different one."""
    return runtime.store.load_config().crowdsec.allowlist


def _edit_allowlist(
    edit: Callable[[Store, HAProxyClient, str], list[str]],
    action: str,
    entry: str,
    runtime: Runtime,
    services: ServicesDep,
) -> list[str]:
    """Run one allowlist edit, audit it once saved, and answer its errors."""
    store = runtime.store
    try:
        entries = edit(store, services.pipeline.client, entry)
    except ChangesPending:
        raise changes_pending() from None
    except HAProxyError as error:
        store.audit.record("admin", action, {"entry": entry})
        message = f"The allowlist is saved, but HAProxy was not updated: {error}. Send it again."
        raise ApiError(502, "haproxy_error", message) from None
    store.audit.record("admin", action, {"entry": entry})
    return entries


@router.post("/security/allowlist", response_model=AllowlistAdded, responses=_ALLOWLIST_RESPONSES)
def add_allowlist_entry(
    body: AllowlistEntry, runtime: Runtime, services: ServicesDep
) -> AllowlistAdded:
    """Add an address or a range, effective at once, and return the allowlist."""
    entries = _edit_allowlist(
        add_to_allowlist, "security.allowlist.add", body.entry, runtime, services
    )
    return AllowlistAdded(
        allowlist=entries, warnings=public_ranges("crowdsec.allowlist", [body.entry])
    )


@router.delete(
    "/security/allowlist/{entry:path}", response_model=list[str], responses=_ALLOWLIST_RESPONSES
)
def remove_allowlist_entry(entry: IpOrCidr, runtime: Runtime, services: ServicesDep) -> list[str]:
    """Remove an address or a range, effective at once, and return the allowlist. A range such as
    `10.0.0.0/8` goes in the path as is. `10.0.0.1` also removes `10.0.0.1/32`."""
    return _edit_allowlist(
        remove_from_allowlist, "security.allowlist.remove", entry, runtime, services
    )
