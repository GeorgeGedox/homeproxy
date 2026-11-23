"""Runtime map updates: an apply that changes only HAProxy's maps needs no reload."""

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from homeproxy.dns.snapshot import FILE_NAME as DNS_FILE
from homeproxy.haproxy.client import HAProxyClient, HAProxyError

logger = logging.getLogger(__name__)

RUNTIME_MAPS = ("hosts.map", "lan-only.map", "hsts.map", "redirect.map")
"""The maps that the runtime API can change. Runtime names are the names in the config."""


@dataclass(frozen=True)
class MapChange:
    """One runtime API command on one map entry."""

    command: Literal["add", "set", "del"]
    map_name: str
    key: str
    value: str = ""


def map_changes(old: Mapping[str, str], new: Mapping[str, str]) -> list[MapChange] | None:
    """Return the runtime map commands that turn the old release into the new one, or None when
    other files differ too. The DNS table does not count, because HAProxy never reads it.
    Additions come first, so a route switched to LAN-only is restricted before it loses HSTS."""
    if old.keys() - {DNS_FILE} != new.keys() - {DNS_FILE}:
        return None
    if any(old[path] != new[path] for path in old if path not in (*RUNTIME_MAPS, DNS_FILE)):
        return None
    additions: list[MapChange] = []
    deletions: list[MapChange] = []
    for map_name in RUNTIME_MAPS:
        before, after = _map_entries(old[map_name]), _map_entries(new[map_name])
        for key, value in after.items():
            if key not in before:
                additions.append(MapChange("add", map_name, key, value))
            elif before[key] != value:
                additions.append(MapChange("set", map_name, key, value))
        deletions += [MapChange("del", map_name, key) for key in before.keys() - after.keys()]
    return additions + deletions


def update_maps(client: HAProxyClient, changes: list[MapChange]) -> bool:
    """Apply map changes at runtime. On failure, log it and return False, so a reload runs."""
    try:
        for change in changes:
            if change.command == "del":
                client.del_map(change.map_name, change.key)
            elif change.command == "set":
                client.set_map(change.map_name, change.key, change.value)
            else:
                client.add_map(change.map_name, change.key, change.value)
    except HAProxyError as exc:
        logger.warning("runtime map update failed, reloading instead: %s", exc)
        return False
    return True


def _map_entries(text: str) -> dict[str, str]:
    entries = {}
    for line in text.splitlines():
        key, _, value = line.partition(" ")
        entries[key] = value
    return entries
