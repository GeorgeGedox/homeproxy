"""The pending changes in plain words, for the pending bar: what changed, never a value.

A route is labelled by what the user sees, its hosts and path or its port, because its name is an
internal ID. Basic auth users are saved live to `secrets.yaml`, so they show only as a difference
between the live userlist and the one the next apply would write. Only the userlist names are read
from it, never a user or a hash.
"""

from collections.abc import Mapping, Sequence

from homeproxy.generator import BACKEND_PREFIX
from homeproxy.models import Action, ChangeSummary, Config, Domain, route_label
from homeproxy.store import StoreError
from homeproxy.store.files import validate_config, validate_route
from homeproxy.store.pending import CONFIG_FILE
from homeproxy.store.tree import parse_tree

# `dashboard` is saved live and holds the password hash, `version` is never edited, and `domains`
# is summarized per zone.
_NOT_SETTINGS = frozenset({"version", "dashboard", "domains"})
_BASIC_AUTH_FALLBACK = "basic auth users"


def change_summary(
    live: Mapping[str, bytes],
    staged: Mapping[str, bytes],
    userlists: tuple[str, str] | None,
) -> list[ChangeSummary]:
    """Return the changes from the live files to the staged overlay, by relative path.

    `userlists` is the live generated userlist file and the one the next apply would write, empty
    when there is none, or None when the overlay cannot be rendered.
    """
    items: list[ChangeSummary] = []
    for path in sorted(live.keys() | staged.keys()):
        if live.get(path) != staged.get(path):
            items += _file_items(path, live.get(path), staged.get(path))
    if userlists is not None:
        items += _basic_auth_items(live, staged, *userlists)
    return items


def _action(before: object, after: object) -> Action:
    if before is None:
        return "added"
    return "removed" if after is None else "changed"


def _file_items(path: str, before: bytes | None, after: bytes | None) -> list[ChangeSummary]:
    if path == CONFIG_FILE and before is not None and after is not None:
        items = _config_items(before, after)
        if items is not None:
            return items
    if path.startswith("routes/"):
        label = _route_label(after if after is not None else before)
        if label is not None:
            return [ChangeSummary(kind="route", action=_action(before, after), label=label)]
    return [ChangeSummary(kind="file", action=_action(before, after), label=path)]


def _route_label(data: bytes | None) -> str | None:
    """Return the label of a route file, or None if it does not parse. The error is not kept,
    because a validation message can quote a value."""
    if data is None:
        return None
    try:
        return route_label(validate_route(parse_tree(data, "route.yaml"), "route.yaml"))
    except StoreError:
        return None


def _config_items(before: bytes, after: bytes) -> list[ChangeSummary] | None:
    try:
        old, new = (validate_config(parse_tree(data, CONFIG_FILE)) for data in (before, after))
    except StoreError:
        return None
    items = _domain_items(old.domains, new.domains)
    for key in Config.model_fields:
        if key not in _NOT_SETTINGS and getattr(old, key) != getattr(new, key):
            items.append(ChangeSummary(kind="settings", action="changed", label=key))
    return items


def _domain_items(before: Sequence[Domain], after: Sequence[Domain]) -> list[ChangeSummary]:
    old = {domain.zone: domain for domain in before}
    new = {domain.zone: domain for domain in after}
    return [
        ChangeSummary(kind="domain", action=_action(old.get(zone), new.get(zone)), label=zone)
        for zone in sorted(old.keys() | new.keys())
        if old.get(zone) != new.get(zone)
    ]


def _basic_auth_items(
    live: Mapping[str, bytes], staged: Mapping[str, bytes], before: str, after: str
) -> list[ChangeSummary]:
    """Return a `changed` item per route whose userlist differs. A route with its own item is
    left out, because that item already says the route changes."""
    old, new = _userlists(before), _userlists(after)
    labels: dict[str, None] = {}
    for name in sorted(old.keys() | new.keys()):
        path = f"routes/{name.removeprefix(BACKEND_PREFIX)}.yaml"
        if old.get(name) == new.get(name) or live.get(path) != staged.get(path):
            continue
        labels[_route_label(staged.get(path)) or _BASIC_AUTH_FALLBACK] = None
    return [ChangeSummary(kind="basic_auth", action="changed", label=label) for label in labels]


def _userlists(text: str) -> dict[str, list[str]]:
    """Return the user lines of each userlist in `15-userlists.cfg`, by userlist name."""
    sections: dict[str, list[str]] = {}
    users: list[str] = []
    for line in text.splitlines():
        if line.startswith("userlist "):
            users = sections.setdefault(line.removeprefix("userlist "), [])
        elif line.lstrip().startswith("user "):
            users.append(line)
    return sections
