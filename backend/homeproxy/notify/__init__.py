"""Notifications: Discord and generic webhook targets, each with the events it picked.

`notify(store, event, title, text)` is the one entry point. It reads the targets from the saved
`config.yaml` and their URLs from `secrets.yaml`, and sends from a background thread, so a dead
receiver never blocks or fails the caller. Every message goes through `redact`.
"""

from homeproxy.notify.payloads import Message, TargetType, payload
from homeproxy.notify.sender import (
    NotifyError,
    mark_delivered,
    notify,
    notify_on_failure,
    post,
    send_once,
)

__all__ = [
    "Message",
    "NotifyError",
    "TargetType",
    "mark_delivered",
    "notify",
    "notify_on_failure",
    "payload",
    "post",
    "send_once",
]
