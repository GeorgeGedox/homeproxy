"""Send messages to the configured targets, off the request and job path."""

import http.client
import json
import logging
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from email.message import Message as Headers
from pathlib import Path
from typing import IO

from homeproxy.models.config import NotificationEvent, NotificationTarget
from homeproxy.notify.payloads import Message, TargetType, payload
from homeproxy.store import Store, StoreError
from homeproxy.util import http as http_request
from homeproxy.util.atomic import atomic_write, ensure_dir
from homeproxy.util.redact import redact

logger = logging.getLogger(__name__)

TIMEOUT = 5.0
RETRIES = 3
BACKOFF = 1.0
"""Seconds before the first retry, doubled before each next one."""
THREAD_NAME = "notify"


class NotifyError(Exception):
    """A message did not reach its receiver. The text is redacted and never holds the URL."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Turns every 3xx into an answer: a webhook that moved is a failed send, never a GET."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: Headers,
        newurl: str,
    ) -> urllib.request.Request | None:
        return None


def send_once(url: str, body: bytes | None = None) -> int:
    """Send `body` as a JSON POST, or a GET without one, and return the HTTP status, whatever it
    is. Redirects are not followed.

    Raises `NotifyError` when no HTTP answer arrives within `TIMEOUT`.
    """
    headers = {"User-Agent": http_request.USER_AGENT}
    if body is not None:
        headers["Content-Type"] = "application/json"
    # The URL is validated as http or https, and urllib picks POST with a body, GET without.
    request = urllib.request.Request(url, data=body, headers=headers)  # noqa: S310
    try:
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=TIMEOUT) as response:
            return int(response.status)
    except urllib.error.HTTPError as error:
        error.close()
        return error.code
    except (OSError, ValueError, http.client.HTTPException) as error:
        # URLError and timeouts are OSErrors, and a receiver that is not HTTP gives an
        # HTTPException.
        raise NotifyError(_reason(error)) from None


def _reason(error: Exception) -> str:
    """Say in plain words why no HTTP answer arrived. The admin reads it after a test send."""
    cause = error.reason if isinstance(error, urllib.error.URLError) else error
    if isinstance(cause, socket.gaierror):
        return "The receiver's host name was not found."
    if isinstance(cause, TimeoutError):
        return f"The receiver did not answer within {TIMEOUT:g} seconds."
    if isinstance(cause, ConnectionRefusedError):
        return "The receiver refused the connection."
    if isinstance(cause, ssl.SSLError):
        return "The receiver's TLS certificate was not accepted."
    if isinstance(cause, http.client.HTTPException):
        return "The receiver did not answer with HTTP."
    return "The receiver could not be reached."


def post(kind: TargetType, url: str, message: Message) -> int:
    """Send `message` once and return the receiver's HTTP status, whatever it is.

    Raises `NotifyError` when no HTTP answer arrives within `TIMEOUT`.
    """
    return send_once(url, json.dumps(payload(kind, message)).encode())


def _send(target: NotificationTarget, url: str, message: Message) -> bool:
    """Send with retries and return whether it arrived. A failure is logged with the target's
    name, never its URL."""
    delay = BACKOFF
    problem = ""
    for attempt in range(RETRIES + 1):
        if attempt:
            time.sleep(delay)
            delay *= 2
        try:
            status = post(target.type, url, message)
        except NotifyError as error:
            problem = str(error)
            continue
        if 200 <= status < 300:
            return True
        problem = f"HTTP {status}"
    logger.warning(
        "notification %s to %s failed after %d tries: %s",
        message.event,
        target.name,
        RETRIES + 1,
        problem,
    )
    return False


def _send_safely(target: NotificationTarget, url: str, message: Message) -> bool:
    """`_send`, at the thread boundary: an error one target causes never stops the others."""
    try:
        return _send(target, url, message)
    except Exception as error:
        logger.error(
            "notification %s to %s failed: %s",
            message.event,
            target.name,
            redact(f"{type(error).__name__}: {error}"),
        )
        return False


def _deliver(store: Store, message: Message, delivered: Callable[[], None] | None) -> None:
    if not store.configured():
        return  # before setup there are no targets
    try:
        config = store.load_config()
        targets = [t for t in config.notifications.targets if message.event in t.events]
        if not targets:
            return
        # Loading secrets registers them, so redacting after it catches every one.
        urls = store.load_secrets().notifications
    except (OSError, StoreError) as error:
        logger.error("cannot read the notification targets for %s: %s", message.event, error)
        return
    message = message.redacted()
    arrived = False
    for target in targets:
        url = urls.target_url(target.name)
        if url:
            arrived = _send_safely(target, url, message) or arrived
        else:
            logger.warning("notification target %s has no URL", target.name)
    if arrived and delivered is not None:
        delivered()


def notify(
    store: Store,
    event: NotificationEvent,
    title: str,
    text: str,
    *,
    delivered: Callable[[], None] | None = None,
    recovered: bool = False,
) -> None:
    """Send a message to every target that lists `event`, as the saved config names them.

    Returns at once: a background thread sends, with `TIMEOUT` and `RETRIES`. Never raises, so it
    can never fail or delay an apply or a job. Every secret is redacted from the message.
    `delivered` runs in that thread once at least one target accepted the message. `recovered`
    sends it as the `info` message that the event's state ended.
    """
    message = Message(event, title, text, datetime.now(UTC), recovered)
    try:
        threading.Thread(
            target=_deliver, args=(store, message, delivered), name=THREAD_NAME, daemon=True
        ).start()
    except RuntimeError as error:
        logger.error("cannot send notification %s: %s", event, error)


def notify_on_failure(
    store: Store, event: NotificationEvent, title: str, func: Callable[[], object]
) -> None:
    """Run `func`. If it raises, notify `event` with the error as the text and raise it again."""
    try:
        func()
    except Exception as error:
        notify(store, event, title, str(error) or "failed")
        raise


def mark_delivered(path: Path, content: bytes = b"") -> None:
    """Write `path` as the record that a target got a message, for `notify`'s `delivered`, so the
    job does not send it again. A failed write is logged."""
    try:
        ensure_dir(path.parent)
        atomic_write(path, content)
    except OSError as error:
        logger.error("cannot record the notification in %s: %s", path, error)
