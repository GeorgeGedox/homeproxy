"""The HAProxy-side CrowdSec allowlist, `crowdsec.allowlist` in `config.yaml`, live in `allow.lst`.

An edit needs no apply. It is saved to `config.yaml`, swapped into HAProxy's runtime ACL, and
written to the live release's `allow.lst`, so a reload or a restart keeps it and the next apply
renders the same file. Both edits are idempotent: when HAProxy cannot be reached, the change is
already saved, and the same call again brings HAProxy in line.
"""

import ipaddress
from collections.abc import Callable

from ruamel.yaml.comments import CommentedMap, CommentedSeq

from homeproxy.generator.render import ALLOW_LIST, allow_list
from homeproxy.haproxy.client import HAProxyClient
from homeproxy.haproxy.release_links import live_release
from homeproxy.store import Store, StoreError, load_tree, validate_config
from homeproxy.store.pending import PendingChanges
from homeproxy.store.snapshots import Snapshots
from homeproxy.util.atomic import atomic_write

CROWDSEC_SPOE = "crowdsec-spoe.cfg"
"""A release has this file only while CrowdSec is on, the only time HAProxy loads `allow.lst`."""


class ChangesPending(StoreError):
    """`config.yaml` is staged. It holds the old allowlist, and an apply would undo the change."""

    def __init__(self) -> None:
        super().__init__("apply or discard the pending change to config.yaml first")


def add_to_allowlist(store: Store, client: HAProxyClient, entry: str) -> list[str]:
    """Add an address or range and return the allowlist. A listed one is not added twice.

    Raises `ValidationFailed` for an entry that is not a valid address or range, `ChangesPending`,
    or `HAProxyError` after the change was saved.
    """

    def add(entries: CommentedSeq) -> None:
        if _network(entry) not in map(_network, entries):
            entries.append(entry)

    return _update(store, client, add)


def remove_from_allowlist(store: Store, client: HAProxyClient, entry: str) -> list[str]:
    """Remove an address or range and return the allowlist. `10.0.0.1` also removes
    `10.0.0.1/32`. Raises as `add_to_allowlist` does."""

    def remove(entries: CommentedSeq) -> None:
        for index in reversed(range(len(entries))):
            if _network(entries[index]) == _network(entry):
                del entries[index]

    return _update(store, client, remove)


def _update(
    store: Store, client: HAProxyClient, change: Callable[[CommentedSeq], None]
) -> list[str]:
    """Edit the allowlist in `config.yaml`, then bring HAProxy and the live release in line."""
    with store.lock() as held:
        if PendingChanges(store).config_staged(held):
            raise ChangesPending()
        tree = load_tree(store.config_path)
        validate_config(tree, store.config_path.name)
        crowdsec = tree.setdefault("crowdsec", CommentedMap())
        change(crowdsec.setdefault("allowlist", CommentedSeq()))
        Snapshots(store).save_live_config(tree, "edit crowdsec.allowlist", held)
        entries = validate_config(tree, store.config_path.name).crowdsec.allowlist
        _update_haproxy(store, client, entries)
    return entries


def _update_haproxy(store: Store, client: HAProxyClient, entries: list[str]) -> None:
    """Swap the entries into HAProxy, then into the live release, under the pipeline's lock."""
    live = live_release(store.root)
    if not (live / ALLOW_LIST).is_file():
        return  # A release from before `allow.lst`. The next apply renders it.
    data = allow_list(entries)
    patterns = data.split()
    if (live / CROWDSEC_SPOE).is_file():
        if patterns:
            client.replace_acl(ALLOW_LIST, patterns)
        else:
            client.clear_acl(ALLOW_LIST)
    atomic_write(live / ALLOW_LIST, data.encode("ascii"), mode=0o644)


def _network(value: object) -> object:
    """Return the range a value names, so `10.0.0.1` matches `10.0.0.1/32`, or else the value."""
    try:
        return ipaddress.ip_network(str(value), strict=True)
    except ValueError:
        return value
