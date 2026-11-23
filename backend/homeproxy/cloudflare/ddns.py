"""Keep each domain's DDNS record on the WAN address.

HomeProxy owns an A record on the DDNS name when its comment is exactly
`managed-by:homeproxy ddns:<zone>`. It creates that record when the name has no A or CNAME, and
updates only owned records. Other record types on the name, such as MX or TXT on a zone apex,
stay as they are. One hand-made A record waits for the admin to adopt it. A CNAME, several
hand-made A records, or an owned A next to a hand-made one block every write. Reconcile and purge
never touch the DDNS record, because they only change records marked `route:`.
"""

import http.client
import ipaddress
import logging
import urllib.error
from dataclasses import dataclass
from typing import Literal

from homeproxy.certs.providers import CLOUDFLARE
from homeproxy.cloudflare.client import (
    CloudflareClient,
    CloudflareError,
    CloudflareRecord,
    RecordData,
)
from homeproxy.cloudflare.reconcile import MARKER
from homeproxy.models import Config, Domain, Secrets
from homeproxy.models.domain import cloudflare_off_reason
from homeproxy.util import http as http_request

log = logging.getLogger(__name__)

WAN_IP_URL = "https://1.1.1.1/cdn-cgi/trace"
"""Cloudflare's trace page names the caller's address. Its IPv4 host gives the IPv4 address."""
FETCH_TIMEOUT = 15.0
DDNS_TAG = f"{MARKER} ddns:"


class DdnsError(Exception):
    """The DDNS record was not changed. The message is safe to show."""


@dataclass(frozen=True)
class DdnsResult:
    """What one zone's DDNS record holds after a run, and what the run did to it.

    `hand-made` means one A record HomeProxy does not own, which `adopt` can take over.
    `blocked` means the name has records adoption cannot fix, and `error` says which.
    """

    zone: str
    record: str
    action: Literal["created", "updated", "unchanged", "adopted", "hand-made", "blocked", "failed"]
    content: str = ""
    error: str = ""


def public_ip(config: Config, url: str = WAN_IP_URL) -> str:
    """Return `network.public_ip`, or with `auto`, the IPv4 address `url` reports."""
    if config.network.public_ip != "auto":
        return config.network.public_ip
    try:
        with http_request.urlopen(url, timeout=FETCH_TIMEOUT) as response:
            text = response.read(4096).decode()
    except urllib.error.HTTPError as error:
        raise DdnsError(f"the WAN address check answered HTTP {error.code}") from None
    except OSError as error:
        log.warning("WAN address check: %s", type(error).__name__)
        raise DdnsError(
            "the WAN address could not be read, check the internet connection"
        ) from None
    except (ValueError, http.client.HTTPException) as error:
        log.warning("WAN address check: %s", type(error).__name__)
        raise DdnsError("the WAN address check sent an answer HomeProxy could not read") from None
    fields = dict(line.partition("=")[::2] for line in text.splitlines())
    try:
        return str(ipaddress.IPv4Address(fields.get("ip", "")))
    except ValueError:
        raise DdnsError("the WAN address check gave no IPv4 address") from None


def update_ddns(config: Config, secrets: Secrets, *, wan_url: str = WAN_IP_URL) -> list[DdnsResult]:
    """Point the DDNS record of every Cloudflare domain with `ddns_update` at the WAN address.

    Does nothing while the integration is off. Raises `DdnsError` before any write if the WAN
    address is unknown. A zone that fails is reported in its result and the others still run.
    """
    domains = [
        d for d in config.domains if d.cloudflare.ddns_update and d.dns_provider == CLOUDFLARE
    ]
    if not config.cloudflare.enabled or not domains:
        return []
    ip = public_ip(config, wan_url)
    results = []
    for domain in domains:
        try:
            results.append(_update(domain, secrets, ip))
        except CloudflareError as error:
            log.warning("cloudflare ddns %s: %s", domain.zone, error)
            results.append(DdnsResult(domain.zone, domain.ddns_record, "failed", error=str(error)))
    return results


def adopt(config: Config, secrets: Secrets, zone: str) -> DdnsResult:
    """Mark the one hand-made A record on the zone's DDNS name as owned, so DDNS may update it.

    Only the comment changes. The address follows on the next DDNS run. Raises `DdnsError` if
    the integration is off, the zone is unknown, or the name has no record to adopt or is
    blocked, and `CloudflareError` if a call fails.
    """
    if not config.cloudflare.enabled:
        raise DdnsError("the Cloudflare integration is off")
    domain = next((d for d in config.domains if d.zone == zone), None)
    if domain is None:
        raise DdnsError(f"{zone} is not a configured domain")
    reason = cloudflare_off_reason(domain)
    if reason:
        raise DdnsError(reason)
    client, zone_id, records = _records(domain, secrets)
    owned, foreign = _split(records, domain)
    reason = _blocked(records, owned, foreign, domain)
    if reason:
        raise DdnsError(reason)
    if owned:
        return DdnsResult(zone, domain.ddns_record, "unchanged", owned[0].content)
    if not foreign:
        raise DdnsError(f"{domain.ddns_record} has no A record to adopt")
    record = foreign[0]
    client.update_record(zone_id, record.id, _data(domain, record.content, record))
    log.info("cloudflare ddns %s: adopted %s", zone, record.name)
    return DdnsResult(zone, domain.ddns_record, "adopted", record.content)


def _update(domain: Domain, secrets: Secrets, ip: str) -> DdnsResult:
    client, zone_id, records = _records(domain, secrets)
    name = domain.ddns_record
    owned, foreign = _split(records, domain)
    reason = _blocked(records, owned, foreign, domain)
    if reason:
        log.warning("cloudflare ddns %s: %s", domain.zone, reason)
        return DdnsResult(domain.zone, name, "blocked", error=reason)
    if foreign:
        return DdnsResult(domain.zone, name, "hand-made", foreign[0].content)
    if not owned:
        client.create_record(zone_id, _data(domain, ip, None))
        log.info("cloudflare ddns %s: created %s at %s", domain.zone, name, ip)
        return DdnsResult(domain.zone, name, "created", ip)
    keep = next((r for r in owned if r.content == ip), owned[0])
    for extra in owned:
        if extra is not keep:  # Cloudflare refuses two identical records, so extras go
            client.delete_record(zone_id, extra.id)
    if keep.content != ip:
        client.update_record(zone_id, keep.id, _data(domain, ip, keep))
    if len(owned) == 1 and keep.content == ip:
        return DdnsResult(domain.zone, name, "unchanged", ip)
    log.info("cloudflare ddns %s: %s now points at %s", domain.zone, name, ip)
    return DdnsResult(domain.zone, name, "updated", ip)


def _records(
    domain: Domain, secrets: Secrets
) -> tuple[CloudflareClient, str, list[CloudflareRecord]]:
    """Return a client, the zone id and the records on the zone's DDNS name."""
    client = CloudflareClient.for_domain(domain, secrets)
    zone_id = client.zone_id(domain.zone)
    records = [r for r in client.list_records(zone_id) if r.name == domain.ddns_record]
    return client, zone_id, records


def _split(
    records: list[CloudflareRecord], domain: Domain
) -> tuple[list[CloudflareRecord], list[CloudflareRecord]]:
    """Return the owned and the hand-made A records."""
    a_records = [r for r in records if r.type == "A"]
    owned = [r for r in a_records if r.comment == f"{DDNS_TAG}{domain.zone}"]
    return owned, [r for r in a_records if r not in owned]


def _blocked(
    records: list[CloudflareRecord],
    owned: list[CloudflareRecord],
    foreign: list[CloudflareRecord],
    domain: Domain,
) -> str:
    """Return why no write may happen on the DDNS name, or an empty string."""
    name = domain.ddns_record
    if any(r.type == "CNAME" for r in records):
        return f"{name} is a CNAME, so it cannot hold the DDNS address"
    if len(foreign) > 1:
        return f"{name} has {len(foreign)} A records that HomeProxy does not own"
    if foreign and owned:
        return f"{name} has A records that HomeProxy owns and one that it does not"
    return ""


def _data(domain: Domain, content: str, record: CloudflareRecord | None) -> RecordData:
    """Return an owned A record, keeping the `proxied` and TTL of an existing record."""
    proxied, ttl = (record.proxied, record.ttl) if record is not None else (False, 1)
    return RecordData("A", domain.ddns_record, content, proxied, f"{DDNS_TAG}{domain.zone}", ttl)
