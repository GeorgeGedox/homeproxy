"""The message and its two formats: Discord (content plus one embed) and a generic JSON webhook."""

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal

from homeproxy.models.config import NotificationEvent
from homeproxy.util.redact import redact

Severity = Literal["error", "warning", "info"]
TargetType = Literal["discord", "webhook"]

SEVERITY: dict[str, Severity] = {
    "certificate_expiring": "warning",
    "new_version": "info",
    "test": "info",
}
"""Every other event is an `error`."""

_DISCORD_COLOR: dict[Severity, int] = {"error": 0xE53935, "warning": 0xFB8C00, "info": 0x1E88E5}
# Discord refuses longer embed titles and descriptions, and longer content.
_TITLE_LIMIT = 256
_TEXT_LIMIT = 4000


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


@dataclass(frozen=True)
class Message:
    """One notification. `event` is a `NotificationEvent`, or `test` for a test send.

    `recovered` marks the message that a down state ended, which is `info` whatever the event.
    """

    event: NotificationEvent | Literal["test"]
    title: str
    text: str
    time: datetime
    recovered: bool = False

    @property
    def severity(self) -> Severity:
        """How bad the event is."""
        return "info" if self.recovered else SEVERITY.get(self.event, "error")

    def redacted(self) -> "Message":
        """Return the message with every registered secret replaced, cut to Discord's limits."""
        title = _cut(redact(self.title), _TITLE_LIMIT)
        return replace(self, title=title, text=_cut(redact(self.text), _TEXT_LIMIT))


def payload(kind: TargetType, message: Message) -> dict[str, object]:
    """Return the JSON body for a target of type `kind`."""
    if kind == "discord":
        embed = {
            "title": message.title,
            "description": message.text,
            "color": _DISCORD_COLOR[message.severity],
            "timestamp": message.time.isoformat(),
        }
        return {
            "content": f"HomeProxy: {message.title}",
            "embeds": [embed],
            # An error text can hold @everyone or a role mention. Ping no one.
            "allowed_mentions": {"parse": []},
        }
    return {
        # Slack, Mattermost and most self-hosted receivers show `text`.
        "text": f"HomeProxy: {message.title}\n{message.text}",
        "event": message.event,
        "severity": message.severity,
        "title": message.title,
        "message": message.text,
        "time": message.time.isoformat(),
    }
