"""Parse HAProxy access lines, as the generated config sends them to the collector.

HAProxy sends each line as one RFC 5424 syslog datagram. The message is HAProxy's default
`httplog` or `tcplog` line, so the CrowdSec HAProxy parser still reads it, followed by three quoted
fields: the route name, the client's country code and the TLS SNI. An empty field is `""`.

The SNI comes from the client. HAProxy URL-encodes it before it reaches the line, so it can hold
no space, quote or control character. It is kept in that encoded form, which is safe to show.
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timezone
from typing import Literal

HTTP_LOG_FORMAT = (
    "%ci:%cp [%tr] %ft %b/%s %TR/%Tw/%Tc/%Tr/%Ta %ST %B %CC %CS %tsc "
    "%ac/%fc/%bc/%sc/%rc %sq/%bq %hr %hs %{+Q}r"
)
"""HAProxy's default `httplog` format, which the CrowdSec HAProxy parser expects."""

TCP_LOG_FORMAT = "%ci:%cp [%t] %ft %b/%s %Tw/%Tc/%Tt %B %ts %ac/%fc/%bc/%sc/%rc %sq/%bq"
"""HAProxy's default `tcplog` format."""

_SYSLOG = re.compile(
    r"<(?P<priority>[0-9]{1,3})>1 (?P<timestamp>\S+) \S+ (?P<app>\S+) \S+ \S+ (?:-|\[.*?\]) "
    r"(?P<message>.*)",
    re.DOTALL,
)
_START = (
    r"(?P<client>\S+):(?P<client_port>[0-9]+|-) \[(?P<accepted>[^\]]+)\] "
    r"(?P<frontend>\S+) (?P<backend>\S+)/(?P<server>\S+) "
)
_COUNTERS = r"[0-9]+/[0-9]+/[0-9]+/[0-9]+/\+?[0-9]+ [0-9]+/[0-9]+ "
_APPENDED = r'"(?P<route>[^"]*)" "(?P<country>[^"]*)" "(?P<sni>[^"]*)"'
_HTTP = re.compile(
    _START + r"-?[0-9]+/-?[0-9]+/-?[0-9]+/-?[0-9]+/\+?(?P<total>-?[0-9]+) (?P<status>-?[0-9]+) "
    r"\+?(?P<bytes>[0-9]+) \S+ \S+ (?P<termination>\S+) "
    + _COUNTERS
    # Header captures are only present when a config adds them.
    + r'(?:\{[^}]*\} )?(?:\{[^}]*\} )?"(?P<request>[^"]*)" '
    + _APPENDED
)
_TCP = re.compile(
    _START
    + r"-?[0-9]+/-?[0-9]+/\+?(?P<total>-?[0-9]+) \+?(?P<bytes>[0-9]+) (?P<termination>\S+) "
    + _COUNTERS
    + _APPENDED
)
_ACCEPTED = re.compile(
    r"(?P<day>[0-9]{2})/(?P<month>[A-Z][a-z]{2})/(?P<year>[0-9]{4}):"
    r"(?P<hour>[0-9]{2}):(?P<minute>[0-9]{2}):(?P<second>[0-9]{2})\.(?P<millisecond>[0-9]{3})"
)
_MONTHS = {
    name: number
    for number, name in enumerate(
        ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"),
        start=1,
    )
}
_TLS_FRONTEND_MARK = "~"
_PRIORITY_MAX = 191


@dataclass(frozen=True, slots=True)
class AccessLog:
    """One request (`http`) or connection (`tcp`) that HAProxy logged.

    Times are in milliseconds. `None` means HAProxy logged no value. `frontend` has no TLS mark:
    `tls` tells whether the client connection used TLS.
    """

    kind: Literal["http", "tcp"]
    logged_at: datetime
    accepted_at: datetime
    client_ip: str
    client_port: int | None
    frontend: str
    tls: bool
    backend: str
    server: str
    total_time_ms: int | None
    status: int | None
    bytes_sent: int
    termination_state: str
    method: str | None
    request: str | None
    route: str | None
    country: str | None
    sni: str | None
    line: str


def _optional(value: str) -> str | None:
    return value or None


def _non_negative(value: str) -> int | None:
    number = int(value)
    return None if number < 0 else number


def _accepted_at(value: str, tz: timezone) -> datetime | None:
    """HAProxy writes the accept time in its local time, with no offset. The syslog timestamp has
    one, and both come from the same clock, so its offset applies."""
    match = _ACCEPTED.fullmatch(value)
    if match is None or match["month"] not in _MONTHS:
        return None
    try:
        return datetime(
            int(match["year"]),
            _MONTHS[match["month"]],
            int(match["day"]),
            int(match["hour"]),
            int(match["minute"]),
            int(match["second"]),
            int(match["millisecond"]) * 1000,
            tzinfo=tz,
        )
    except ValueError:
        return None


def _syslog_time(value: str) -> datetime | None:
    try:
        logged_at = datetime.fromisoformat(value)
    except ValueError:
        return None
    return logged_at if logged_at.tzinfo is not None else None


def parse_message(message: str, logged_at: datetime) -> AccessLog | None:
    """Parse the message of one syslog datagram. Return None if it is not an access line.

    `logged_at` is the syslog timestamp and must hold a UTC offset. HAProxy's own notices, such as
    a server going down, are not access lines.
    """
    message = message.rstrip("\n")
    kind: Literal["http", "tcp"] = "http"
    match = _HTTP.fullmatch(message)
    if match is None:
        kind = "tcp"
        match = _TCP.fullmatch(message)
    if match is None:
        return None
    offset = logged_at.utcoffset()
    accepted_at = _accepted_at(match["accepted"], timezone(offset) if offset is not None else UTC)
    if accepted_at is None:
        return None
    # An IPv6 address holds colons too. The pattern takes the port after the last one.
    client_port = match["client_port"]
    frontend = match["frontend"]
    tls = frontend.endswith(_TLS_FRONTEND_MARK)
    request: str | None = None
    method: str | None = None
    status: int | None = None
    if kind == "http":
        request = match["request"]
        # A request HAProxy could not parse is logged as `<BADREQ>`, with no method.
        method, separator, _ = request.partition(" ")
        method = method if separator and method else None
        status = _non_negative(match["status"])
    return AccessLog(
        kind=kind,
        logged_at=logged_at,
        accepted_at=accepted_at,
        client_ip=match["client"],
        client_port=None if client_port == "-" else int(client_port),
        frontend=frontend.removesuffix(_TLS_FRONTEND_MARK),
        tls=tls,
        backend=match["backend"],
        server=match["server"],
        total_time_ms=_non_negative(match["total"]),
        status=status,
        bytes_sent=int(match["bytes"]),
        termination_state=match["termination"],
        method=method,
        request=request,
        route=_optional(match["route"]),
        country=_optional(match["country"]),
        sni=_optional(match["sni"]),
        line=message,
    )


def parse_syslog(data: bytes) -> tuple[datetime, str] | None:
    """Return the timestamp and message of one syslog datagram from HAProxy, or None when it is
    not one.

    Bytes that are not UTF-8 are replaced, so any input gives a result and never an exception.
    """
    match = _SYSLOG.fullmatch(data.decode("utf-8", errors="replace"))
    if match is None or int(match["priority"]) > _PRIORITY_MAX:
        return None
    logged_at = _syslog_time(match["timestamp"])
    if logged_at is None:
        return None
    return logged_at, match["message"]


__all__ = [
    "HTTP_LOG_FORMAT",
    "TCP_LOG_FORMAT",
    "AccessLog",
    "parse_message",
    "parse_syslog",
]
