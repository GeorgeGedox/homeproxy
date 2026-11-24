"""A client for the CrowdSec LAPI that lists, adds and deletes decisions with machine credentials.

Each call logs in first (`POST /v1/watchers/login`), then sends one request with the token.
Decisions are listed through the alerts that hold them, as `cscli decisions list` does, because
`GET /v1/decisions` takes a bouncer key. A new decision goes in a manual alert, as with
`cscli decisions add`. The password is registered for redaction and only ever sent in a body. The
token is only sent in a header, and logging in again for each call keeps no token in memory.
"""

import http.client
import ipaddress
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal

from homeproxy.util import http as http_request
from homeproxy.util.redact import redact, register_secret

log = logging.getLogger(__name__)

TIMEOUT = 15.0
MANUAL_ORIGIN = "cscli"
"""The origin CrowdSec gives a decision made by hand."""
_GO_DURATION_PART = re.compile(r"([0-9.]+)(ms|us|µs|ns|h|m|s)")
_GO_UNITS = {"h": 3600.0, "m": 60.0, "s": 1.0, "ms": 1e-3, "us": 1e-6, "µs": 1e-6, "ns": 1e-9}

DecisionType = Literal["ban", "captcha"]


class LapiError(Exception):
    """A LAPI call failed or was refused. The message never holds the password or the token."""


BLOCKLIST_ORIGINS: Final = frozenset({"CAPI", "lists"})
"""Origins of blocklist decisions, which can number tens of thousands."""


@dataclass(frozen=True)
class Decision:
    """One active decision. `scope` is `Ip` or `Range`, `origin` is `crowdsec` for a local
    detection, `cscli` for a manual decision, and `CAPI` or `lists` for a blocklist."""

    id: int
    type: str
    scope: str
    value: str
    origin: str
    scenario: str
    expires_at: datetime
    created_at: datetime
    """When its alert reached the LAPI. For a blocklist, the pull that brought it."""


class LapiClient:
    """Calls one LAPI as one machine."""

    def __init__(self, url: str, machine_id: str, password: str) -> None:
        """Use the LAPI at `url`, such as `http://127.0.0.1:18080`, as `machine_id`."""
        register_secret(password)
        self._base = url.rstrip("/")
        self._machine_id = machine_id
        self._password = password

    def decisions(self) -> list[Decision]:
        """Return every active decision, blocklists included, newest alert first."""
        query = "has_active_decision=true&include_capi=true&limit=0"
        alerts = self._call("GET", f"/v1/alerts?{query}")
        now = datetime.now(UTC)
        try:
            found = [
                _decision(item, now, datetime.fromisoformat(alert["created_at"]))
                for alert in alerts or []
                for item in alert["decisions"] or []
            ]
        except (KeyError, TypeError, ValueError) as error:
            log.warning("crowdsec decisions: unexpected answer: %r", error)
            raise LapiError("unexpected answer listing decisions") from None
        return [d for d in found if d is not None]

    def add_decision(self, value: str, kind: DecisionType, duration: timedelta) -> None:
        """Ban or captcha an IP address or range for `duration`.

        Raises `ValueError`, before anything is sent, for another kind, a value that is not an
        address or a range, a catch-all range, an IPv4-mapped IPv6 value, or a duration under one
        second.
        """
        if kind not in ("ban", "captcha"):
            raise ValueError("a decision is a ban or a captcha")
        network = ipaddress.ip_network(value, strict=True)
        if network.prefixlen == 0:
            raise ValueError("a catch-all range would match every client")
        if network.version == 6 and network.network_address.ipv4_mapped is not None:
            raise ValueError("write an IPv4-mapped address as IPv4")
        seconds = int(duration.total_seconds())
        if seconds < 1:
            raise ValueError("a decision lasts at least one second")
        single = network.num_addresses == 1
        scope, target = ("Ip", str(network.network_address)) if single else ("Range", str(network))
        reason = f"manual '{kind}' from homeproxy"
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        decision = {"duration": f"{seconds}s", "origin": MANUAL_ORIGIN, "scenario": reason}
        decision |= {"scope": scope, "type": kind, "value": target}
        alert = {
            "capacity": 0,
            "decisions": [decision],
            "events": [],
            "events_count": 1,
            "leakspeed": "0",
            "message": reason,
            "remediation": True,
            "scenario": reason,
            "scenario_hash": "",
            "scenario_version": "",
            "simulated": False,
            "source": {"scope": scope, "value": target, "ip": target},
            # The LAPI counts the duration from `stop_at`.
            "start_at": now,
            "stop_at": now,
        }
        self._call("POST", "/v1/alerts", [alert])

    def delete_decision(self, decision_id: int) -> None:
        """Delete one decision. A blocklist can bring its value back at its next pull."""
        self._call("DELETE", f"/v1/decisions/{int(decision_id)}")

    def login(self) -> str:
        """Log in as the machine and return the token. Raises `LapiError`."""
        login = {"machine_id": self._machine_id, "password": self._password}
        answer = self._send("POST", "/v1/watchers/login", login, None)
        token = answer.get("token") if isinstance(answer, dict) else None
        if not isinstance(token, str) or not token:
            raise LapiError("the LAPI login answered without a token")
        return token

    def _call(self, method: str, path: str, payload: object = None) -> Any:
        """Log in, then return the JSON body of one successful call."""
        return self._send(method, path, payload, self.login())

    def _send(self, method: str, path: str, payload: object, token: str | None) -> Any:
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(  # noqa: S310 - the URL comes from the validated config
            f"{self._base}{path}",
            data=None if payload is None else json.dumps(payload).encode(),
            method=method,
            headers=headers,
        )
        name = urllib.parse.urlsplit(path).path
        try:
            with http_request.urlopen(request, timeout=TIMEOUT) as response:
                log.info("crowdsec %s %s: HTTP %s", method, name, response.status)
                return json.load(response)
        except urllib.error.HTTPError as error:
            with error:
                log.info("crowdsec %s %s: HTTP %s", method, name, error.code)
                reason = _message(error)
            raise LapiError(f"{method} {name} failed: {reason}") from None
        except OSError as error:
            log.warning("crowdsec %s %s: %s", method, name, type(error).__name__)
            raise LapiError(
                "the CrowdSec API could not be reached, check the CrowdSec API URL and that "
                "CrowdSec is running"
            ) from None
        except (ValueError, http.client.HTTPException) as error:
            log.warning("crowdsec %s %s: %s", method, name, type(error).__name__)
            raise LapiError("the CrowdSec API sent an answer HomeProxy could not read") from None


def _decision(item: dict[str, Any], now: datetime, created_at: datetime) -> Decision | None:
    """Return an active decision, or None for an expired or simulated one."""
    left = _go_duration(str(item["duration"]))
    if left <= timedelta(0) or item.get("simulated"):
        return None
    return Decision(
        id=int(item["id"]),
        type=str(item["type"]),
        scope=str(item["scope"]),
        value=str(item["value"]),
        origin=str(item["origin"]),
        scenario=str(item.get("scenario") or ""),
        expires_at=now + left,
        created_at=created_at,
    )


def _go_duration(text: str) -> timedelta:
    """Parse a Go duration such as `3h59m50.5s` or `-1m2s`. Raises `ValueError`."""
    sign, body = (-1, text[1:]) if text.startswith("-") else (1, text)
    parts = _GO_DURATION_PART.findall(body)
    if "".join(number + unit for number, unit in parts) != body:
        raise ValueError(f"not a duration: {text[:40]}")
    return timedelta(seconds=sign * sum(float(n) * _GO_UNITS[u] for n, u in parts))


def _message(error: urllib.error.HTTPError) -> str:
    """Return the LAPI's own error message, or the HTTP status."""
    try:
        body = json.load(error)
    except (OSError, ValueError, http.client.HTTPException):
        body = None
    message = body.get("message") if isinstance(body, dict) else None
    return redact(str(message))[:500] if message else f"HTTP {error.code}"
