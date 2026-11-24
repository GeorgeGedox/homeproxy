"""Route endpoints: CRUD in the pending change set, editor validation and site settings.

Route edits are staged and reach HAProxy at the next apply. Basic auth users (`api.basic_auth`) are
not: they are written straight to `secrets.yaml`, which has no pending copy, and take effect at the
next apply. So a rename copies the users to the new name and a delete keeps them, and a discard
leaves a route that still applies. The apply drops the users of routes that are gone.
An edit rewrites only the top-level keys whose value changed, so comments in a hand-written route
file stay, and an edit that changes nothing stages nothing. A save of an `http` route also gives its
site settings (exposure, Cloudflare, DNS) to the other routes of its site.
"""

import copy
import logging
import socket
from collections.abc import Iterable, Sequence
from dataclasses import replace
from typing import Annotated, Any, Self

from fastapi import APIRouter, Body, Response
from pydantic import BaseModel, model_validator
from ruamel.yaml.comments import CommentedMap

from homeproxy.api.apply import ServicesDep
from homeproxy.api.changes import edit_lock
from homeproxy.api.countries import check_added_countries
from homeproxy.api.errors import ApiError, not_found
from homeproxy.auth.routes import Runtime
from homeproxy.haproxy.apply_rules import HOST_SETTINGS, check_rules, host_group, path_only_hosts
from homeproxy.models import (
    HttpRoute,
    PassthroughRoute,
    RedirectRoute,
    Route,
    StrictModel,
    TcpRoute,
    parse_route,
    route_label,
)
from homeproxy.models.fields import Name
from homeproxy.models.route import Exposure
from homeproxy.models.route_name import new_route_name
from homeproxy.store import (
    HeldLock,
    InvalidRouteName,
    Store,
    StoreError,
    ValidationFailed,
    dump_tree,
    load_tree,
    validate_route,
)
from homeproxy.store.pending import Overlay, PendingChanges
from homeproxy.store.tree import parse_tree

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["routes"])

RouteBody = Annotated[
    HttpRoute | PassthroughRoute | TcpRoute | RedirectRoute, Body(discriminator="type")
]


# A draft has no `name` until `_named` gives it one. Only POST bodies take a draft; the route file
# and PUT keep `name` required, hence the narrowed override of the inherited field.
class HttpRouteDraft(HttpRoute):
    """An `http` route to create. With no `name`, the server makes one."""

    name: Name | None = None  # type: ignore[assignment]


class PassthroughRouteDraft(PassthroughRoute):
    """A `passthrough` route to create. With no `name`, the server makes one."""

    name: Name | None = None  # type: ignore[assignment]


class TcpRouteDraft(TcpRoute):
    """A `tcp` route to create. With no `name`, the server makes one."""

    name: Name | None = None  # type: ignore[assignment]


class RedirectRouteDraft(RedirectRoute):
    """A `redirect` route to create. With no `name`, the server makes one."""

    name: Name | None = None  # type: ignore[assignment]


RouteDraft = Annotated[
    HttpRouteDraft | PassthroughRouteDraft | TcpRouteDraft | RedirectRouteDraft,
    Body(discriminator="type"),
]

UPSTREAM_TIMEOUT = 2.0
"""Seconds the editor's reachability check waits for each upstream."""

_NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"description": "No route has this name"}}
_BUSY: dict[int | str, dict[str, Any]] = {
    409: {"description": "The name is taken, or an apply is running"}
}


class RouteCheck(BaseModel):
    """Problems the next apply would refuse (`errors`) or only warn about (`warnings`)."""

    errors: list[str]
    warnings: list[str]


class RouteSaved(BaseModel):
    """The saved route, the warnings the next apply gives for its hosts, and the label of each other
    route of its site that the save gave its site settings."""

    route: Route
    warnings: list[str]
    site_changed: list[str]


class HostWarning(BaseModel):
    """A host the next apply warns about, and the warning."""

    host: str
    message: str


class HostSettings(StrictModel):
    """New settings for a host, which every route of its site shares. A null field keeps its value.
    `cloudflare_dns` is the `cloudflare.dns` switch; `proxied` keeps its value."""

    exposure: Exposure | None = None
    cloudflare_dns: bool | None = None

    @model_validator(mode="after")
    def _sets_something(self) -> Self:
        if self.exposure is None and self.cloudflare_dns is None:
            raise ValueError("set exposure or cloudflare_dns")
        return self


class HostSaved(BaseModel):
    """The routes of the host's site after the change, and the warnings the next apply gives for
    their hosts."""

    routes: list[Route]
    warnings: list[str]


def _path(store: Store, name: str) -> str:
    """Return the pending path of a route, or answer 404 for a name no route can have."""
    try:
        store.route_path(name)
    except InvalidRouteName:
        raise not_found("route", "name") from None
    return f"routes/{name}.yaml"


def read_route(store: Store, name: str, held: HeldLock) -> tuple[CommentedMap, Route]:
    """Return a route's tree and model as the next apply sees them, or answer 404."""
    data = PendingChanges(store).read(_path(store, name), held)
    if data is None:
        raise not_found("route", "name")
    tree = parse_tree(data, f"{name}.yaml")
    return tree, validate_route(tree, f"{name}.yaml")


def _merge(tree: CommentedMap, old: Route | None, new: Route) -> CommentedMap:
    """Write the top-level keys of `new` whose value changed, leaving defaults implicit."""
    before = {} if old is None else old.model_dump(mode="json")
    after = new.model_dump(mode="json")
    explicit = new.model_dump(mode="json", exclude_defaults=True)
    for key in [key for key in tree if key not in after]:
        del tree[key]
    for key, value in after.items():
        if key in before and before[key] == value:
            continue
        if key in explicit:
            tree[key] = explicit[key]
        else:
            tree.pop(key, None)
    return tree


def _check_countries(services: ServicesDep, store: Store, old: list[str], new: Route) -> None:
    field = "geoip.block_countries"
    check_added_countries(services.jobs, store.root, field, old, new.geoip.block_countries)


def _copy_basic_auth(store: Store, name: str, new_name: str, held: HeldLock) -> None:
    """Give the renamed route the users of its old name. The old entry stays until an apply."""
    if name not in store.load_secrets().basic_auth:
        return
    tree = load_tree(store.secrets_path)
    tree["basic_auth"][new_name] = copy.deepcopy(tree["basic_auth"][name])
    store.save_secrets(tree, held)


def _name_taken(store: Store, name: str, held: HeldLock) -> bool:
    """Return whether a live or pending route has this name.

    A live route counts as taken even when its delete is staged: its basic auth users stay until
    the apply, and a new route of that name would get them.
    """
    path = _path(store, name)
    return store.route_path(name).exists() or PendingChanges(store).read(path, held) is not None


def _rename_taken(store: Store, name: str, new_name: str, held: HeldLock) -> bool:
    """Return whether a rename to `new_name` must be refused.

    A live route whose delete is staged keeps its users until the apply, so only a route with the
    same users, such as the one renamed away from that name, may take it.
    """
    if PendingChanges(store).read(_path(store, new_name), held) is not None:
        return True
    users = store.load_secrets().basic_auth
    return new_name in users and users[new_name] != users.get(name)


def _named(store: Store, draft: Route, held: HeldLock) -> Route:
    """Return the draft as a route, under its own name or a new one that no route has."""
    name = draft.name or new_route_name(draft, lambda name: _name_taken(store, name, held))
    return parse_route(draft.model_dump() | {"name": name})


def _path_warnings(routes: Iterable[Route], route: Route) -> list[str]:
    """Return the warnings for the route's hosts that are routed only on paths."""
    warnings = path_only_hosts(routes)
    hosts = [] if isinstance(route, TcpRoute) else route.hosts
    return [warnings[host] for host in hosts if host in warnings]


def _saved(store: Store, route: Route, held: HeldLock, site_changed: list[str]) -> RouteSaved:
    """Return the saved route with the warnings for its hosts, as the next apply sees them."""
    try:
        routes = PendingChanges(store).load_overlay(held).routes
    except StoreError as error:
        # Another file is broken. The route is staged all the same, and the apply names the file.
        logger.info("route %s saved without its warnings: %s", route.name, error)
        return RouteSaved(route=route, warnings=[], site_changed=site_changed)
    warnings = _path_warnings(routes, route)
    return RouteSaved(route=route, warnings=warnings, site_changed=site_changed)


def _site_settings(routes: Sequence[Route], route: Route) -> list[tuple[Route, Route]]:
    """Return each other route of the route's site whose host settings differ, with its copy that
    takes them from the route. Only `http` routes share a host."""
    if not isinstance(route, HttpRoute):
        return []
    settings = {name: getattr(route, name) for name in HOST_SETTINGS}
    return [
        (other, other.model_copy(update=settings))
        for other in host_group(routes, route.hosts[0])
        if isinstance(other, HttpRoute)
        and other is not route
        and any(getattr(other, name) != value for name, value in settings.items())
    ]


def _site_edits(
    pending: PendingChanges, route: Route, replaces: str, held: HeldLock
) -> dict[str, tuple[str, bytes]]:
    """Return the checked file and the label of each other route of the site that saving `route`
    in place of `replaces` changes, by path. Nothing is staged."""
    try:
        overlay = pending.load_overlay(held)
    except StoreError as error:
        # Another file is broken: the apply names it, and the site waits for it.
        logger.info("route %s saved without its site: %s", route.name, error)
        return {}
    path_of = {id(other): path for path, other in overlay.route_files.items()}
    edits = {}
    for old, new in _site_settings(_with_route(overlay, route, replaces).routes, route):
        path = path_of[id(old)]
        edits[path] = (route_label(new), _host_edit(pending, path, overlay.files[path], old, new))
    return edits


def _stage_site(
    pending: PendingChanges, edits: dict[str, tuple[str, bytes]], held: HeldLock
) -> list[str]:
    """Stage the files `_site_edits` checked and return their labels."""
    for path, (_, data) in edits.items():
        pending.stage(path, data, held)
    return [label for label, _ in edits.values()]


@router.get("/routes", response_model=list[Route])
def list_routes(runtime: Runtime) -> list[Route]:
    """Return every route as the next apply would see it, pending edits included."""
    with edit_lock(runtime.store) as held:
        return list(PendingChanges(runtime.store).load_overlay(held).routes)


@router.post("/routes", status_code=201, response_model=RouteSaved, responses=_BUSY)
def create_route(draft: RouteDraft, runtime: Runtime, services: ServicesDep) -> RouteSaved:
    """Stage a new route. It goes live at the next apply. With no `name`, the server makes one from
    the first host and path, or the port of a `tcp` route."""
    store = runtime.store
    pending = PendingChanges(store)
    with edit_lock(store) as held:
        body = _named(store, draft, held)
        path = _path(store, body.name)
        if _name_taken(store, body.name, held):
            raise ApiError(409, "route_exists", "A route has this name.", field="name")
        _check_countries(services, store, [], body)
        edits = _site_edits(pending, body, body.name, held)
        pending.stage_tree(path, _merge(CommentedMap(), None, body), held)
        return _saved(store, body, held, _stage_site(pending, edits, held))


@router.get("/routes/warnings", response_model=list[HostWarning])
def list_route_warnings(runtime: Runtime) -> list[HostWarning]:
    """Return the hosts the next apply warns about because their enabled routes all serve a path
    below `/`, sorted by host."""
    with edit_lock(runtime.store) as held:
        try:
            routes = PendingChanges(runtime.store).load_overlay(held).routes
        except StoreError as error:
            # A broken file: the apply names it.
            logger.info("route warnings not checked: %s", error)
            return []
    warnings = sorted(path_only_hosts(routes).items())
    return [HostWarning(host=host, message=message) for host, message in warnings]


@router.get("/routes/{name}", response_model=Route, responses=_NOT_FOUND)
def get_route(name: str, runtime: Runtime) -> Route:
    """Return one route, pending edits included."""
    with edit_lock(runtime.store) as held:
        return read_route(runtime.store, name, held)[1]


@router.put("/routes/{name}", response_model=RouteSaved, responses=_NOT_FOUND | _BUSY)
def update_route(name: str, body: RouteBody, runtime: Runtime, services: ServicesDep) -> RouteSaved:
    """Stage a changed route. A new `name` renames it, and its basic auth users move with it."""
    store = runtime.store
    pending = PendingChanges(store)
    with edit_lock(store) as held:
        tree, old = read_route(store, name, held)
        path = _path(store, body.name)
        renamed = body.name != name
        if renamed and _rename_taken(store, name, body.name, held):
            raise ApiError(409, "route_exists", "A route has this name.", field="name")
        # An unchanged route still sets its site settings, so a save mends a split site.
        edits = _site_edits(pending, body, name, held)
        if body != old:
            _check_countries(services, store, old.geoip.block_countries, body)
            pending.stage_tree(path, _merge(tree, old, body), held)
            if renamed:
                pending.stage_delete(_path(store, name), held)
                _copy_basic_auth(store, name, body.name, held)
        return _saved(store, body, held, _stage_site(pending, edits, held))


@router.delete("/routes/{name}", status_code=204, response_class=Response, responses=_NOT_FOUND)
def delete_route(name: str, runtime: Runtime) -> Response:
    """Stage the removal of a route. Its file is not validated, so a broken hand-edited route can
    be removed. Its basic auth users go at the apply that removes it."""
    store = runtime.store
    pending = PendingChanges(store)
    with edit_lock(store) as held:
        path = _path(store, name)
        if pending.read(path, held) is None:
            raise not_found("route", "name")
        pending.stage_delete(path, held)
    return Response(status_code=204)


def _with_route(overlay: Overlay, route: Route | None, replaces: str) -> Overlay:
    """Return the overlay with `route`, if any, in place of the route named `replaces`, if any.

    `route` comes last, so a clash names the other route as the one already there.
    """
    by_path = dict(overlay.route_files)
    by_path.pop(f"routes/{replaces}.yaml", None)
    if route is not None:
        path = f"routes/{route.name}.yaml"
        by_path.pop(path, None)
        by_path[path] = route
    files = {p: d for p, d in overlay.files.items() if p not in overlay.route_files}
    files |= dict.fromkeys(by_path, b"")
    return replace(overlay, route_files=by_path, files=files)


def _reachable(address: str) -> bool:
    host, _, port = address.rpartition(":")
    try:
        with socket.create_connection((host, int(port)), timeout=UPSTREAM_TIMEOUT):
            return True
    except OSError:
        return False


@router.post("/routes/validate", response_model=RouteCheck)
def check_route(draft: RouteDraft, runtime: Runtime, name: str | None = None) -> RouteCheck:
    """Check a draft route against the other routes and the config: host inside a domain, no
    duplicate host, no port clash, not the dashboard. An unreachable upstream and a host routed only
    on paths are warnings.

    `name` is the route being edited, when it is renamed. A draft with no name is checked under the
    name a create would give it. Nothing is staged.
    """
    store = runtime.store
    with edit_lock(store) as held:
        overlay = PendingChanges(store).load_overlay(held)
        body = _named(store, draft, held)
    routes = _with_route(overlay, body, name or body.name)
    site = {id(old): new for old, new in _site_settings(routes.routes, body)}
    edited = {path: site.get(id(route), route) for path, route in routes.route_files.items()}
    routes = replace(routes, route_files=edited)
    # The draft's errors are those the config without it does not have.
    before = set(check_rules(_with_route(overlay, None, name or body.name), store.load_secrets))
    errors = [error for error in check_rules(routes, store.load_secrets) if error not in before]
    warnings = [
        f"upstream {upstream.address} is not reachable"
        for upstream in ([] if isinstance(body, RedirectRoute) else body.upstreams)
        if not _reachable(upstream.address)
    ]
    warnings += _path_warnings(routes.routes, body)
    return RouteCheck(errors=errors, warnings=warnings)


def _with_host_settings(route: Route, settings: HostSettings) -> Route:
    """Return the route with the host's new settings."""
    update: dict[str, Any] = {}
    if settings.exposure is not None:
        update["exposure"] = settings.exposure
    # A tcp route has no hosts, so it is never in a host's group.
    if settings.cloudflare_dns is not None and not isinstance(route, TcpRoute):
        update["cloudflare"] = route.cloudflare.model_copy(update={"dns": settings.cloudflare_dns})
    return route.model_copy(update=update)


def _host_edit(pending: PendingChanges, path: str, data: bytes, old: Route, new: Route) -> bytes:
    """Return the route file with the change, checked as `stage` checks it. A failed check names
    the file."""
    pending.check_path(path)  # A hand-written file such as `routes/app_v2.yaml` cannot be staged.
    file = path.removeprefix("routes/")
    edited = dump_tree(_merge(parse_tree(data, file), old, new), file)
    try:
        pending.validate(path, edited)
    except ValidationFailed as error:
        loc = error.errors[0]["loc"] if error.errors else ()
        field = ".".join(str(segment) for segment in loc) or None
        raise ApiError(422, "validation_failed", str(error), field=field) from None
    return edited


# `main` includes `api.basic_auth` first, so a route named `hosts` keeps its basic auth endpoint.
@router.put(
    "/routes/hosts/{host}",
    response_model=HostSaved,
    responses={404: {"description": "No route has this host"}, 409: _BUSY[409]},
)
def update_host(host: str, settings: HostSettings, runtime: Runtime) -> HostSaved:
    """Stage new settings for a host on every route that must agree on them: the routes on `host`
    and the `http` routes joined to them through shared hosts. Every file is checked before the
    first is staged, so all of them are staged or none is."""
    store = runtime.store
    pending = PendingChanges(store)
    with edit_lock(store) as held:
        overlay = pending.load_overlay(held)
        group = host_group(overlay.routes, host)
        if not group:
            raise ApiError(404, "host_not_found", "No route has this host.")
        path_of = {id(route): path for path, route in overlay.route_files.items()}
        changed = [_with_host_settings(route, settings) for route in group]
        edits: dict[str, bytes] = {}
        for old, new in zip(group, changed, strict=True):
            if new != old:
                path = path_of[id(old)]
                edits[path] = _host_edit(pending, path, overlay.files[path], old, new)
        for path, data in edits.items():
            pending.stage(path, data, held)
    # Exposure and Cloudflare do not change which hosts are routed only on paths.
    warnings = [warning for route in changed for warning in _path_warnings(overlay.routes, route)]
    return HostSaved(routes=changed, warnings=list(dict.fromkeys(warnings)))
