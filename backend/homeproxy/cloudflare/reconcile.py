"""Make each zone's route records in Cloudflare match the routes.

A public, enabled route with its switch on gets a CNAME from each host to its domain's DDNS
record. Ownership is the record comment: reconcile creates records with `managed-by:homeproxy
route:<name>` and only ever updates or deletes records that carry that marker. Any other record
on a route's host is a hand-made record, and the host is reported as a conflict instead.

Only domains with `dns_provider: cloudflare` take part. A host in a domain on another provider
gets no record, whatever its route's switch says.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from homeproxy.certs.providers import CLOUDFLARE
from homeproxy.cloudflare.client import (
    CloudflareClient,
    CloudflareError,
    CloudflareRecord,
    RecordData,
)
from homeproxy.models import (
    Config,
    Domain,
    HttpRoute,
    PassthroughRoute,
    RedirectRoute,
    Route,
    Secrets,
)
from homeproxy.models.domain import cloudflare_off_reason, domain_of
from homeproxy.store import Store, StoreError
from homeproxy.store.snapshots import Snapshots

log = logging.getLogger(__name__)

MARKER = "managed-by:homeproxy"
"""Starts the comment of every record HomeProxy owns."""
ROUTE_TAG = f"{MARKER} route:"


class ReconcileError(Exception):
    """Reconcile did not run, so nothing in Cloudflare changed."""


@dataclass(frozen=True)
class Change:
    """One create, update or delete of a route record. An update also has the old values."""

    action: Literal["create", "update", "delete"]
    name: str
    content: str
    proxied: bool
    route: str
    record_id: str = ""
    old_content: str = ""
    old_proxied: bool = False


@dataclass(frozen=True)
class Conflict:
    """A route host that has a hand-made record, which reconcile never touches."""

    name: str
    route: str


@dataclass
class ZoneResult:
    """The plan for one zone and, unless it was a dry run, what failed while applying it.

    `records` are the records HomeProxy owned in the zone before the run, DDNS records included.
    """

    zone: str
    records: list[CloudflareRecord] = field(default_factory=list)
    changes: list[Change] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _Desired:
    name: str
    content: str
    proxied: bool
    route: str


def reconcile(
    config: Config, routes: Sequence[Route], secrets: Secrets, *, dry_run: bool = False
) -> list[ZoneResult]:
    """Plan, and unless `dry_run`, apply the route records of every zone.

    Does nothing while the integration is off. A zone that fails is reported in its result and
    the other zones still run.
    """
    if not config.cloudflare.enabled:
        return []
    return _sync(config, routes, secrets, dry_run)


def reconcile_store(store: Store, *, dry_run: bool = False) -> list[ZoneResult]:
    """Run `reconcile` on `/data/config`, which must equal the newest snapshot.

    Raises `ReconcileError` before any call if a file fails to load, so a broken route file can
    never look like a deleted route. It also raises if `/data/config` differs from the newest
    snapshot: a hand edit is not live in HAProxy yet, and records follow only what is live.
    """
    try:
        config, routes, secrets = store.load_config(), store.load_routes(), store.load_secrets()
    except (StoreError, OSError) as error:
        message = f"The config could not be loaded, so no record changed: {error}"
        raise ReconcileError(message) from None
    if not Snapshots(store).is_applied():
        message = "Hand edits to the config are not applied yet, so no record changed."
        message += " Apply them first."
        raise ReconcileError(message)
    return reconcile(config, routes, secrets, dry_run=dry_run)


def purge(config: Config, secrets: Secrets) -> list[ZoneResult]:
    """Delete every route record HomeProxy owns, even while the integration is off.

    Hand-made records and the DDNS record stay.
    """
    return _sync(config, [], secrets, dry_run=False)


ADOPTABLE_TYPES = ("A", "AAAA", "CNAME")
"""Record types that a route record can take over. The next reconcile replaces an A or AAAA."""


def adopt_host(config: Config, routes: Sequence[Route], secrets: Secrets, host: str) -> str:
    """Mark the one hand-made record on a route's host as owned and return the route's name.

    Only the comment changes. The next reconcile replaces an A or AAAA record with the route's
    CNAME and updates a CNAME in place.
    Raises `ReconcileError` if the integration is off, no applied public route with its switch on
    uses the host, or the host does not hold exactly one hand-made A, AAAA or CNAME record, and
    `CloudflareError` if a call fails.
    """
    if not config.cloudflare.enabled:
        raise ReconcileError("the Cloudflare integration is off")
    domain = domain_of(host, config.domains)
    reason = domain and cloudflare_off_reason(domain)
    want = (
        next((w for w in _desired(config, routes).get(domain.zone, []) if w.name == host), None)
        if domain
        else None
    )
    if domain is None or want is None:
        raise ReconcileError(f"no applied public route with a Cloudflare record uses {host}")
    if reason:
        raise ReconcileError(reason)
    client = CloudflareClient.for_domain(domain, secrets)
    zone_id = client.zone_id(domain.zone)
    records = [r for r in client.list_records(zone_id) if r.name == host]
    if len(records) != 1:
        raise ReconcileError(
            f"{host} has {len(records) or 'no'} records, so there is no one to adopt"
        )
    record = records[0]
    if record.comment.startswith(MARKER):
        raise ReconcileError(f"{host} is already managed by HomeProxy")
    if record.type not in ADOPTABLE_TYPES:
        raise ReconcileError(f"{host} holds a {record.type} record, which a route cannot take over")
    data = RecordData(record.type, host, record.content, record.proxied, _tag(want), record.ttl)
    client.update_record(zone_id, record.id, data)
    log.info("cloudflare reconcile %s: adopted %s", domain.zone, host)
    return want.route


def _sync(
    config: Config, routes: Sequence[Route], secrets: Secrets, dry_run: bool
) -> list[ZoneResult]:
    desired = _desired(config, routes)
    hosts = {want.name for wanted in desired.values() for want in wanted}
    results = []
    for domain in config.domains:
        if domain.dns_provider != CLOUDFLARE:
            continue
        result = ZoneResult(domain.zone)
        try:
            _sync_zone(domain, desired.get(domain.zone, []), hosts, secrets, dry_run, result)
        except CloudflareError as error:
            result.errors.append(str(error))
        for message in result.errors:
            log.warning("cloudflare reconcile %s: %s", domain.zone, message)
        results.append(result)
    return results


def _sync_zone(
    domain: Domain,
    desired: list[_Desired],
    hosts: set[str],
    secrets: Secrets,
    dry_run: bool,
    result: ZoneResult,
) -> None:
    client = CloudflareClient.for_domain(domain, secrets)
    zone_id = client.zone_id(domain.zone)
    records = client.list_records(zone_id)
    result.records = [r for r in records if r.comment.startswith(MARKER)]
    result.changes, result.conflicts = _plan(desired, hosts, records)
    if dry_run:
        return
    for change in result.changes:
        try:
            _apply(client, zone_id, change)
        except CloudflareError as error:
            result.errors.append(f"{change.name}: {error}")


def _desired(config: Config, routes: Sequence[Route]) -> dict[str, list[_Desired]]:
    """Return the records the routes ask for, by zone. Routes on one host by path agree on its
    record, so the first of them names it."""
    desired: dict[str, list[_Desired]] = {}
    seen: set[str] = set()
    for route in routes:
        if not isinstance(route, HttpRoute | PassthroughRoute | RedirectRoute):
            continue  # tcp routes have no host name to point at
        if not (route.enabled and route.exposure == "public" and route.cloudflare.dns):
            continue
        for host in route.hosts:
            domain = domain_of(host, config.domains)
            if domain is None or host == domain.ddns_record or host in seen:
                continue  # outside every domain the apply fails; the DDNS record is not a CNAME
            seen.add(host)
            proxied = route.cloudflare.proxied
            if proxied is None:
                proxied = domain.cloudflare.default_proxied
            record = _Desired(host, domain.ddns_record, proxied, route.name)
            desired.setdefault(domain.zone, []).append(record)
    return desired


def _plan(
    desired: list[_Desired], hosts: set[str], records: list[CloudflareRecord]
) -> tuple[list[Change], list[Conflict]]:
    """Compare the desired records with the zone's records. Only route records are changed.

    An owned record on a host that any zone wants is never deleted, because a nested domain can
    claim a host whose record lives in the parent zone. Deletes come first, so a new CNAME never
    collides with a record that is about to go.
    """
    ours = [r for r in records if r.comment.startswith(ROUTE_TAG)]
    hand_made = {r.name for r in records if not r.comment.startswith(ROUTE_TAG)}
    upserts: list[Change] = []
    replaced: list[Change] = []
    conflicts: list[Conflict] = []
    kept: set[str] = set()
    for want in desired:
        if want.name in hand_made:
            conflicts.append(Conflict(want.name, want.route))
            continue
        have = next((r for r in ours if r.name == want.name), None)
        if have is not None and have.type != "CNAME":
            # An adopted A or AAAA is replaced: Cloudflare may not change a type, and an A and a
            # CNAME cannot share a name, so the delete runs before the create.
            replaced.append(_delete(have))
            have = None
        if have is None:
            upserts.append(Change("create", want.name, want.content, want.proxied, want.route))
            continue
        kept.add(have.id)
        if (have.content, have.proxied, have.comment) != (want.content, want.proxied, _tag(want)):
            upserts.append(
                Change(
                    "update", want.name, want.content, want.proxied, want.route, have.id,
                    have.content, have.proxied,
                )
            )  # fmt: skip
    deletes = [_delete(r) for r in ours if r.id not in kept and r.name not in hosts]
    return deletes + replaced + upserts, conflicts


def _delete(record: CloudflareRecord) -> Change:
    comment = record.comment.removeprefix(ROUTE_TAG)
    return Change("delete", record.name, record.content, record.proxied, comment, record.id)


def _tag(want: _Desired | Change) -> str:
    return f"{ROUTE_TAG}{want.route}"


def _apply(client: CloudflareClient, zone_id: str, change: Change) -> None:
    data = RecordData("CNAME", change.name, change.content, change.proxied, _tag(change))
    if change.action == "create":
        client.create_record(zone_id, data)
    elif change.action == "update":
        client.update_record(zone_id, change.record_id, data)
    else:
        client.delete_record(zone_id, change.record_id)
