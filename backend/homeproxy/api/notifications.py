"""Notification URLs and test sends.

The targets themselves (name, type, events) are the `notifications` section of `/settings`, staged
like every other setting. Their URLs are secrets in `secrets.yaml`: `PUT /notifications/urls` sets,
replaces or clears them at once, and a read says only whether each one is set. Logs and the audit
log name the targets, never a URL.
"""

import logging
from datetime import UTC, datetime

from fastapi import APIRouter
from pydantic import BaseModel, Field
from ruamel.yaml.comments import CommentedMap

from homeproxy.api.changes import edit_lock
from homeproxy.api.errors import ApiError
from homeproxy.auth.routes import Runtime
from homeproxy.models import StrictModel
from homeproxy.models.config import NotificationTarget
from homeproxy.models.fields import Name
from homeproxy.models.secrets import NotificationUrl
from homeproxy.notify import Message, NotifyError, post, send_once
from homeproxy.store import Store, load_tree
from homeproxy.store.files import validate_config
from homeproxy.store.pending import CONFIG_FILE, PendingChanges

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["notifications"])


class NotificationUrlsInput(StrictModel):
    """URLs to set, replace or clear. Send `""` to clear one, and leave a URL out to keep it."""

    targets: dict[Name, NotificationUrl] = Field(default_factory=dict)
    heartbeat_url: NotificationUrl | None = None


class NotificationUrlsSet(BaseModel):
    """Whether each URL is set. URLs are never returned."""

    targets: dict[str, bool] = Field(
        description="Each target of the settings, with pending changes, and whether it has a URL"
    )
    unused: list[str] = Field(
        description="Names that have a URL but no target, live or pending, for example after a "
        'rename is applied. Clear them with `{"targets": {"<name>": ""}}`'
    )
    heartbeat_url: bool


class NotificationTestResult(BaseModel):
    """What the receiver answered to a test message."""

    status: int | None = Field(description="The receiver's HTTP status, or null with no answer")
    error: str | None = Field(description="Why no answer arrived, or null")


def _staged_targets(store: Store) -> list[NotificationTarget]:
    with edit_lock(store) as held:
        tree = PendingChanges(store).read_tree(CONFIG_FILE, held)
    return validate_config(tree).notifications.targets


def _urls_set(store: Store) -> NotificationUrlsSet:
    urls = store.load_secrets().notifications
    names = {t.name for t in _staged_targets(store)}
    # Until an apply, a target renamed in the pending changes still sends under its live name.
    live = {t.name for t in store.load_config().notifications.targets}
    return NotificationUrlsSet(
        targets={name: bool(urls.target_url(name)) for name in sorted(names)},
        unused=sorted(urls.targets.keys() - names - live),
        heartbeat_url=bool(urls.heartbeat_url.get_secret_value()),
    )


def _child(parent: CommentedMap, key: str) -> CommentedMap:
    found = parent.get(key)
    if not isinstance(found, CommentedMap):
        found = parent[key] = CommentedMap()
    return found


def _set(section: CommentedMap, key: str, value: object, keep: bool) -> None:
    if keep:
        section[key] = value
    else:
        section.pop(key, None)


def _write(tree: CommentedMap, body: NotificationUrlsInput) -> list[str]:
    """Write the URLs the request sent into `tree` and return the names that changed."""
    section = _child(tree, "notifications")
    targets = _child(section, "targets")
    for name, url in body.targets.items():
        value = url.get_secret_value()
        _set(targets, name, CommentedMap(url=value), bool(value))
    if body.heartbeat_url is not None:
        value = body.heartbeat_url.get_secret_value()
        _set(section, "heartbeat_url", value, bool(value))
    _set(section, "targets", targets, bool(targets))
    _set(tree, "notifications", section, bool(section))
    return sorted(body.targets) + ([] if body.heartbeat_url is None else ["heartbeat_url"])


@router.get(
    "/notifications/urls",
    response_model=NotificationUrlsSet,
    responses={401: {"description": "No session"}},
)
def get_urls(runtime: Runtime) -> NotificationUrlsSet:
    """Return whether each target and the heartbeat have a URL, never a URL."""
    return _urls_set(runtime.store)


@router.put(
    "/notifications/urls",
    response_model=NotificationUrlsSet,
    responses={
        401: {"description": "No valid session"},
        422: {
            "description": "A URL is not an http or https URL; `field` names it. No value is echoed"
        },
    },
)
def put_urls(body: NotificationUrlsInput, runtime: Runtime) -> NotificationUrlsSet:
    """Set, replace or clear URLs at once, and return which are set."""
    store = runtime.store
    with edit_lock(store) as held:
        tree = load_tree(store.secrets_path)
        changed = _write(tree, body)
        if changed:
            store.save_secrets(tree, held)
    if changed:
        logger.info("notification URLs changed: %s", ", ".join(changed))
        store.audit.record("admin", "notification_urls", {"changed": changed})
    return _urls_set(store)


@router.post(
    "/notifications/targets/{name}/test",
    response_model=NotificationTestResult,
    responses={
        401: {"description": "No valid session"},
        404: {"description": "No target has this name"},
        409: {"description": "The target has no URL"},
    },
)
def test_target(name: str, runtime: Runtime) -> NotificationTestResult:
    """Send one sample message to a target of the settings, with pending changes, and return
    what its receiver answered. It is sent once, with no retry."""
    target = next((t for t in _staged_targets(runtime.store) if t.name == name), None)
    if target is None:
        raise ApiError(404, "target_not_found", "No notification target has this name.")
    url = runtime.store.load_secrets().notifications.target_url(name)
    if not url:
        raise ApiError(409, "url_not_set", "Set the target's URL first.")
    message = Message(
        "test",
        "Test message",
        "Notifications from HomeProxy reach this channel.",
        datetime.now(UTC),
    )
    try:
        status = post(target.type, url, message)
    except NotifyError as error:
        logger.warning("test notification to %s failed: %s", name, error)
        return NotificationTestResult(status=None, error=str(error))
    logger.info("test notification to %s: HTTP %d", name, status)
    return NotificationTestResult(status=status, error=None)


@router.post(
    "/notifications/heartbeat/test",
    response_model=NotificationTestResult,
    responses={
        401: {"description": "No valid session"},
        409: {"description": "The heartbeat has no ping URL"},
    },
)
def test_heartbeat(runtime: Runtime) -> NotificationTestResult:
    """Send one ping to the heartbeat URL and return what it answered, whether the heartbeat is
    on or not. It is sent once, with no retry."""
    url = runtime.store.load_secrets().notifications.heartbeat_url.get_secret_value()
    if not url:
        raise ApiError(409, "url_not_set", "Set the heartbeat's ping URL first.")
    try:
        status = send_once(url)
    except NotifyError as error:
        logger.warning("test heartbeat ping failed: %s", error)
        return NotificationTestResult(status=None, error=str(error))
    logger.info("test heartbeat ping: HTTP %d", status)
    return NotificationTestResult(status=status, error=None)
