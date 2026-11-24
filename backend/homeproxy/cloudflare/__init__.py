"""Cloudflare DNS: the API client, the reconcile of route records, and DDNS.

`reconcile_store` runs after an apply and on a schedule. `reconcile(..., dry_run=True)` on the
pending overlay gives the changes to show before an apply. `adopt_host` hands a hand-made record
on a route's host to reconcile. `purge` removes every route record HomeProxy owns.
`homeproxy.cloudflare.ddns` keeps each domain's DDNS record on the WAN address and adopts a
hand-made one on request; reconcile and purge never touch that record.
"""

from homeproxy.cloudflare.client import (
    CloudflareClient,
    CloudflareError,
    CloudflareRecord,
    RecordData,
)
from homeproxy.cloudflare.reconcile import (
    MARKER,
    Change,
    Conflict,
    ReconcileError,
    ZoneResult,
    adopt_host,
    purge,
    reconcile,
    reconcile_store,
)

__all__ = [
    "MARKER",
    "Change",
    "CloudflareClient",
    "CloudflareError",
    "CloudflareRecord",
    "Conflict",
    "ReconcileError",
    "RecordData",
    "ZoneResult",
    "adopt_host",
    "purge",
    "reconcile",
    "reconcile_store",
]
