"""Internal DNS endpoints: the table the DNS server answers from, and the extra LAN records.

Extra records are `dns_records` in `config.yaml`. An edit is staged in the pending change set and
reaches the DNS server at the next apply.
"""

import logging
from collections.abc import Iterable

from fastapi import APIRouter, Response
from pydantic import BaseModel, ValidationError
from ruamel.yaml.comments import CommentedMap

from homeproxy.api.changes import edit_lock
from homeproxy.api.errors import ApiError
from homeproxy.auth.routes import Runtime
from homeproxy.dns.snapshot import FILE_NAME, Snapshot, load_snapshot
from homeproxy.models import Config, Route, TcpRoute, route_label
from homeproxy.models.config import DnsRecord
from homeproxy.models.domain import domain_of
from homeproxy.store import HeldLock, Store, validate_config
from homeproxy.store.pending import CONFIG_FILE, PendingChanges

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["dns"])


class DnsTable(BaseModel):
    """`served` is the table of the live release, or null before the first apply. `extra` are
    the extra records, pending edits included. A route host wins over an extra record."""

    served: Snapshot | None
    extra: list[DnsRecord]


def _config_tree(store: Store, held: HeldLock) -> tuple[CommentedMap, Config]:
    tree = PendingChanges(store).read_tree(CONFIG_FILE, held)
    return tree, validate_config(tree)


def _served(store: Store) -> Snapshot | None:
    try:
        return load_snapshot(store.root / "generated" / FILE_NAME)
    except FileNotFoundError:
        return None
    except (OSError, ValidationError) as error:
        # Such as a table an older release wrote. The DNS server keeps the one it loaded.
        logger.warning("cannot read the served DNS table: %s", error)
        return None


@router.get("/dns/records", response_model=DnsTable)
def list_records(runtime: Runtime) -> DnsTable:
    """Return the table the DNS server answers from and the extra records."""
    store = runtime.store
    with edit_lock(store) as held:
        extra = _config_tree(store, held)[1].dns_records
    return DnsTable(served=_served(store), extra=extra)


@router.post(
    "/dns/records",
    status_code=201,
    response_model=DnsRecord,
    responses={
        409: {"description": "A record or an enabled route has this name, or an apply is running"},
        422: {"description": "The name is in no domain"},
    },
)
def create_record(body: DnsRecord, runtime: Runtime) -> DnsRecord:
    """Stage an extra record. Its name must be inside a configured domain, and no enabled route
    may have it as a host, because the route's answer would win."""
    store = runtime.store
    with edit_lock(store) as held:
        tree, config = _config_tree(store, held)
        if domain_of(body.name, config.domains) is None:
            message = "The name is in none of your domains. Add its domain on Domains first."
            raise ApiError(422, "outside_domains", message, field="name")
        if any(record.name == body.name for record in config.dns_records):
            raise ApiError(409, "record_exists", "A record has this name already.", field="name")
        _check_route_hosts(body.name, PendingChanges(store).load_overlay(held).routes)
        tree.setdefault("dns_records", []).append(body.model_dump(mode="json"))
        PendingChanges(store).stage_tree(CONFIG_FILE, tree, held)
    return body


def _check_route_hosts(name: str, routes: Iterable[Route]) -> None:
    for route in routes:
        if not isinstance(route, TcpRoute) and route.enabled and name in route.hosts:
            raise ApiError(
                409,
                "route_host",
                f"The site {route_label(route)} answers for this name."
                " Set its address in the site's settings.",
                field="name",
            )


@router.delete(
    "/dns/records/{name}",
    status_code=204,
    response_class=Response,
    responses={404: {"description": "No extra record has this name"}},
)
def delete_record(name: str, runtime: Runtime) -> Response:
    """Stage the removal of an extra record."""
    store = runtime.store
    with edit_lock(store) as held:
        tree, config = _config_tree(store, held)
        records = config.dns_records
        index = next((i for i, record in enumerate(records) if record.name == name), None)
        if index is None:
            raise ApiError(404, "record_not_found", "No extra record has this name.")
        del tree["dns_records"][index]
        PendingChanges(store).stage_tree(CONFIG_FILE, tree, held)
    return Response(status_code=204)
