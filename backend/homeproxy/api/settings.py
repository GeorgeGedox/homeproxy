"""Settings: `GET`/`PUT /settings` for what HAProxy uses, and `/settings/dashboard`.

Every setting in `/settings` affects a generated file, so a change is staged in the pending change
set and takes effect on the next apply. The dashboard settings affect no generated file. They are
written to `config.yaml` at once, like the password, and are refused while edits are pending, whose
staged `config.yaml` would bring the old values back. A new listen address is saved only once the
admin reaches it (`homeproxy.auth.listen`).
"""

import logging
from collections.abc import Callable, Iterable
from dataclasses import replace
from functools import partial
from operator import attrgetter
from typing import Annotated

from fastapi import APIRouter, Request
from pydantic import AfterValidator, BaseModel, Field
from ruamel.yaml.comments import CommentedMap
from starlette.concurrency import run_in_threadpool

from homeproxy.api.apply import ServicesDep
from homeproxy.api.changes import edit_lock
from homeproxy.api.countries import check_added_countries
from homeproxy.api.errors import ApiError, changes_pending
from homeproxy.auth.listen import WINDOW_SCOPE_KEY, ListenRefused, PendingListen
from homeproxy.auth.routes import Runtime
from homeproxy.auth.server import default_route_address, split_listen
from homeproxy.auth.sessions import parse_duration
from homeproxy.haproxy.apply_rules import check_rules, port_clashes, public_range_warnings
from homeproxy.models import Config, Route, StrictModel
from homeproxy.models.config import (
    CertificateSettings,
    CloudflareSettings,
    Crowdsec,
    Dashboard,
    DnsServer,
    Geoip,
    Network,
    Notifications,
    Prometheus,
)
from homeproxy.models.fields import Duration, ListenAddress
from homeproxy.store import Store, StoreError, load_tree
from homeproxy.store.files import validate_config
from homeproxy.store.pending import CONFIG_FILE, PendingChanges
from homeproxy.store.snapshots import Snapshots

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["settings"])


class Settings(StrictModel):
    """The `config.yaml` sections the Settings page edits, as the next apply would see them."""

    network: Network
    dns_server: DnsServer
    cloudflare: CloudflareSettings
    geoip: Geoip
    crowdsec: Crowdsec
    certificates: CertificateSettings
    prometheus: Prometheus
    notifications: Notifications


class SettingsSaved(BaseModel):
    """Whether the save staged a change, and what the next apply will warn about. Nothing was
    refused."""

    changed: bool
    warnings: list[str]


def settings_of(config: Config) -> Settings:
    """Return the settings sections of a config."""
    return Settings(
        network=config.network,
        dns_server=config.dns_server,
        cloudflare=config.cloudflare,
        geoip=config.geoip,
        crowdsec=config.crowdsec,
        certificates=config.certificates,
        prometheus=config.prometheus,
        notifications=config.notifications,
    )


def merge_section(tree: CommentedMap, name: str, old: BaseModel, new: BaseModel) -> bool:
    """Write the keys of one section whose value changed, and return whether any did. Other keys
    keep their text and comments.

    A value equal to the current one is never written, so a key that another endpoint edits, such
    as `geoip.block_countries`, is not touched. A changed key is written without its defaults, and
    a key or section that is back to its default is removed, so defaults stay implicit.
    """
    before = old.model_dump(mode="json")
    after = new.model_dump(mode="json")
    lean = new.model_dump(mode="json", exclude_defaults=True)
    changed = [key for key, value in after.items() if before.get(key) != value]
    if not changed:
        return False
    section = tree.get(name)
    if not isinstance(section, CommentedMap):
        section = tree[name] = CommentedMap()
    for key in changed:
        if lean.get(key) is None:
            section.pop(key, None)
        else:
            section[key] = lean[key]
    if not section:
        del tree[name]
    return True


@router.get("/settings", response_model=Settings)
def get_settings(runtime: Runtime) -> Settings:
    """Return the settings with pending changes applied."""
    with runtime.store.lock() as held:
        tree = PendingChanges(runtime.store).read_tree(CONFIG_FILE, held)
    return settings_of(validate_config(tree))


@router.put(
    "/settings",
    response_model=SettingsSaved,
    responses={
        401: {"description": "No valid session"},
        422: {
            "description": "The settings are not valid or make a port clash; `field` names the "
            "first problem"
        },
    },
)
def put_settings(body: Settings, runtime: Runtime, services: ServicesDep) -> SettingsSaved:
    """Stage new settings in the pending change set. Nothing changes until the next apply.

    Turning the heartbeat on is refused while its ping URL is not set, and a port clash that the
    apply would refuse is refused here too, and so is a blocked country that `PUT /geoip` would
    refuse. A public LAN or allowlist range is only a warning.
    """
    pending = PendingChanges(runtime.store)
    with runtime.store.lock() as held:
        tree = pending.read_tree(CONFIG_FILE, held)
        before = validate_config(tree)
        current = settings_of(before)
        _check_heartbeat(runtime.store, current.notifications, body.notifications)
        check_added_countries(
            services.jobs,
            runtime.store.root,
            "geoip.block_countries",
            current.geoip.block_countries,
            body.geoip.block_countries,
        )
        changed = [
            merge_section(tree, name, getattr(current, name), getattr(body, name))
            for name in Settings.model_fields
        ]
        after = validate_config(tree)
        _check_ports(before, after, lambda: pending.load_overlay(held).routes)
        # Staging an unchanged tree would rewrite a hand-written file in the house style, which
        # shows as a pending change and blocks a password change.
        if any(changed):
            pending.stage_tree(CONFIG_FILE, tree, held)
    return SettingsSaved(changed=any(changed), warnings=public_range_warnings(after))


def _check_heartbeat(store: Store, current: Notifications, new: Notifications) -> None:
    """Answer 422 when `new` turns the heartbeat on and no ping URL is set."""
    if not new.heartbeat.enabled or current.heartbeat.enabled:
        return
    if not store.load_secrets().notifications.heartbeat_url.get_secret_value():
        raise ApiError(
            422,
            "validation_failed",
            "Set the heartbeat URL before you turn the heartbeat on.",
            field="notifications.heartbeat.enabled",
        )


_PORT_FIELDS = {
    "network.http_port": "http_port",
    "network.https_port": "https_port",
    "dns_server.listen": "dns_server",
    "prometheus.listen": "prometheus",
    "prometheus.enabled": "prometheus",
}
"""The settings that hold a port, and how a clash message names each one."""


def _check_ports(before: Config, after: Config, load_routes: Callable[[], Iterable[Route]]) -> None:
    """Answer 422 when `after` adds a port clash. `field` is the changed setting the first clash
    names. Clashes that `before` has already are not this change's fault. The routes are read
    only when a port setting changed, so a broken route file blocks no other setting."""
    changed = [f for f in _PORT_FIELDS if attrgetter(f)(before) != attrgetter(f)(after)]
    if not changed:
        return
    routes = tuple(load_routes())
    old = set(port_clashes(before, routes))
    clashes = [clash for clash in port_clashes(after, routes) if clash not in old]
    if not clashes:
        return
    field = next((f for f in changed if _PORT_FIELDS[f] in clashes[0]), None)
    raise ApiError(422, "port_clash", "; ".join(clashes), field=field)


MIN_SESSION_SECONDS = 300
"""A shorter session would end before the admin finishes a change."""


def _long_enough(value: str) -> str:
    if parse_duration(value) < MIN_SESSION_SECONDS:
        raise ValueError("must be at least 5m")
    return value


class DashboardInput(StrictModel):
    """The dashboard settings to save. A new `listen` is saved only once confirmed."""

    listen: ListenAddress
    session_ttl: Annotated[Duration, AfterValidator(_long_enough)]


class DashboardView(BaseModel):
    """The saved dashboard settings, and a new listen address that waits for its confirmation."""

    listen: str = Field(description="The saved address, used from the next start")
    session_ttl: str
    pending_listen: PendingListen | None = Field(
        description="Open until `expires_at`. Any API call with a session on `url` saves it."
    )


def _view(runtime: Runtime, saved: Dashboard) -> DashboardView:
    return DashboardView(
        listen=saved.listen,
        session_ttl=saved.session_ttl,
        pending_listen=runtime.listen_change.pending,
    )


def _saved_dashboard(store: Store, body: DashboardInput) -> Dashboard:
    """Return the saved dashboard settings. Answer 409 if `body` changes them while edits are
    pending."""
    with edit_lock(store) as held:
        saved = validate_config(load_tree(store.config_path), store.config_path.name).dashboard
        changes = (body.listen, body.session_ttl) != (saved.listen, saved.session_ttl)
        if changes and PendingChanges(store).status(held).changes:
            raise changes_pending()
    return saved


def _save_session_ttl(store: Store, ttl: str) -> Dashboard:
    with edit_lock(store) as held:
        tree = load_tree(store.config_path)
        tree["dashboard"]["session_ttl"] = ttl
        Snapshots(store).save_live_config(tree, "edit dashboard.session_ttl", held)
        saved = validate_config(tree, store.config_path.name).dashboard
    store.audit.record("admin", "dashboard_settings", {"session_ttl": ttl})
    return saved


def _url(listen: str, request: Request) -> str:
    """Return the address the admin's browser can open. For every address (`0.0.0.0`), that is
    the host name in use now."""
    host, port = split_listen(listen)
    if host == "0.0.0.0":  # noqa: S104 - compared, not bound
        host = request.url.hostname or default_route_address() or host
    return f"https://{host}:{port}"


def _check_listen(store: Store, listen: str) -> None:
    """Answer 422 if the next apply would refuse the config with `listen` as the dashboard's
    address, for example a port HAProxy uses or a public route whose upstream it would be.

    HAProxy may be down, so a free port proves nothing. Rules the config breaks already are left
    out: they are not this change's fault.
    """
    with edit_lock(store) as held:
        overlay = PendingChanges(store).load_overlay(held)
    dashboard = overlay.config.dashboard.model_copy(update={"listen": listen})
    draft = replace(overlay, config=overlay.config.model_copy(update={"dashboard": dashboard}))
    before = set(check_rules(overlay, store.load_secrets))
    errors = [error for error in check_rules(draft, store.load_secrets) if error not in before]
    if errors:
        raise ApiError(422, "listen_refused", "; ".join(errors), field="listen")


def _check_overlap(saved: str, listen: str) -> None:
    """Answer 422 when `listen` cannot bind beside the saved address, which keeps listening until
    the move is confirmed: the same port with `0.0.0.0` on either side."""
    saved_host, saved_port = split_listen(saved)
    host, port = split_listen(listen)
    if port != saved_port or "0.0.0.0" not in {saved_host, host}:  # noqa: S104 - compared, not bound
        return
    message = (
        f"The dashboard listens on {saved} until you confirm the move, and {listen} needs the "
        f"same port. Move to another port first, then to {listen}."
    )
    raise ApiError(422, "listen_unavailable", message, field="listen")


def _save_listen(store: Store, listen: str) -> bool:
    """Write a confirmed `dashboard.listen`. Return False, and save nothing, while edits are
    pending or the file cannot be written."""
    try:
        with edit_lock(store) as held:
            if PendingChanges(store).status(held).changes:
                logger.warning("dashboard.listen not confirmed: apply or discard pending changes")
                return False
            tree = load_tree(store.config_path)
            tree["dashboard"]["listen"] = listen
            Snapshots(store).save_live_config(tree, "edit dashboard.listen", held)
    except (OSError, StoreError) as error:
        logger.error("cannot save dashboard.listen: %s", error)
        return False
    store.audit.record("admin", "dashboard_listen", {"listen": listen})
    return True


def _arrived_on(request: Request, listen: str) -> bool:
    """Whether `request` came in on the address `listen` through the listener `homeproxy serve`
    started, not a new address on trial or confirmed since. The server address is the accepted
    socket's own, so a listener on every address gives the IP the client connected to."""
    if WINDOW_SCOPE_KEY in request.scope:
        return False
    server = request.scope.get("server")
    return server is not None and f"{server[0]}:{server[1]}" == listen


async def _save_here(runtime: Runtime, listen: str) -> Dashboard:
    """Save `listen` at once: a logged-in request reached it, which is what a confirmation
    proves. The running listener keeps its address and source filter until a restart."""
    store = runtime.store
    await run_in_threadpool(_check_listen, store, listen)
    await runtime.listen_change.cancel()
    if not await run_in_threadpool(_save_listen, store, listen):
        raise ApiError(503, "save_failed", "dashboard.listen could not be saved. Check the log.")
    logger.info("dashboard.listen saved from the fallback address: %s", listen)
    return store.load_config().dashboard


@router.get("/settings/dashboard", response_model=DashboardView)
def get_dashboard(runtime: Runtime) -> DashboardView:
    """Return the saved dashboard settings and the listen address on trial, if any."""
    return _view(runtime, runtime.store.load_config().dashboard)


@router.put(
    "/settings/dashboard",
    response_model=DashboardView,
    responses={
        401: {"description": "No valid session"},
        409: {"description": "Changes are pending; apply or discard them first"},
        422: {"description": "A value is not valid, or this host cannot listen on `listen`"},
    },
)
async def put_dashboard(body: DashboardInput, request: Request, runtime: Runtime) -> DashboardView:
    """Save the session length at once. Start a new listen address beside the current one.

    The new address is saved only when a request with a session reaches it within 60 s, through
    `pending_listen.url`. Otherwise, or if the dashboard restarts first, the saved one stays.
    Sending the saved address closes a new one on trial. While the dashboard is on its fallback
    address, sending the address this request arrived on saves it at once.
    """
    store = runtime.store
    saved = await run_in_threadpool(_saved_dashboard, store, body)
    change = runtime.listen_change
    if body.listen == saved.listen:
        await change.cancel()
    elif runtime.on_fallback and _arrived_on(request, body.listen):
        saved = await _save_here(runtime, body.listen)
    elif change.pending is None or change.pending.listen != body.listen:
        _check_overlap(saved.listen, body.listen)
        await run_in_threadpool(_check_listen, store, body.listen)
        try:
            url = _url(body.listen, request)
            await change.begin(request.app, body.listen, url, partial(_save_listen, store))
        except ListenRefused as error:
            raise ApiError(422, "listen_unavailable", str(error), field="listen") from None
    if body.session_ttl != saved.session_ttl:
        saved = await run_in_threadpool(_save_session_ttl, store, body.session_ttl)
    return _view(runtime, saved)
