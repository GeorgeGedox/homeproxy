"""Parsers for the text HAProxy returns from `show info` and `show stat`.

These functions only read text, so they need no socket and work on captured output.
Columns are found by name, because new HAProxy versions add columns.
"""

import csv
from collections.abc import Mapping
from dataclasses import dataclass
from enum import IntEnum


class StatsParseError(ValueError):
    """The text is not `show info` or `show stat` output, or a required field is bad."""


class ProxyType(IntEnum):
    """The `type` column of `show stat`."""

    FRONTEND = 0
    BACKEND = 1
    SERVER = 2
    LISTENER = 3


@dataclass(frozen=True)
class HAProxyInfo:
    """The answer to `show info` from the current worker: the fields HomeProxy reads."""

    version: str
    pid: int
    uptime_seconds: int


@dataclass(frozen=True)
class StatRow:
    """One row of `show stat`: the columns HomeProxy reads. A counter is `None` where HAProxy
    leaves the column empty.

    `status` is HAProxy's text, for example `OPEN`, `UP`, `DOWN`, `no check`, `MAINT` or
    a transition such as `UP 1/3`.
    """

    proxy: str
    service: str
    type: ProxyType
    status: str
    responses_5xx: int | None
    requests_total: int | None
    check_status: str


def parse_info(text: str) -> HAProxyInfo:
    """Parse the answer to `show info`.

    Raises:
        StatsParseError: `Version`, `Pid` or `Uptime_sec` is missing or not valid. An
            error message from HAProxy in place of the output ends up here.
    """
    fields: dict[str, str] = {}
    # Not splitlines(), which also splits on characters that are not line ends here.
    for line in text.split("\n"):
        name, separator, value = line.partition(":")
        if separator:
            fields[name] = value.strip()
    version = fields.get("Version")
    if not version:
        raise StatsParseError("show info output has no Version")
    return HAProxyInfo(
        version=version,
        pid=_required_int(fields, "Pid"),
        uptime_seconds=_required_int(fields, "Uptime_sec"),
    )


def parse_stat(text: str) -> list[StatRow]:
    """Parse the CSV answer to `show stat`.

    Raises:
        StatsParseError: The header is missing, a row does not match the header, or a
            column holds a value of the wrong kind. An error message from HAProxy in
            place of the CSV ends up here.
    """
    lines = [line for line in text.split("\n") if line]
    if not lines or not lines[0].startswith("# "):
        raise StatsParseError("show stat output has no header line")
    # Every line ends in a comma, so the last column has an empty name.
    header = lines[0].removeprefix("# ").split(",")
    for required in ("pxname", "svname", "type"):
        if required not in header:
            raise StatsParseError(f"show stat header has no {required} column")
    rows = []
    for number, values in enumerate(csv.reader(lines[1:]), start=2):
        if len(values) != len(header):
            raise StatsParseError(
                f"show stat line {number} has {len(values)} columns, expected {len(header)}"
            )
        fields = {name: value for name, value in zip(header, values, strict=True) if name}
        rows.append(_stat_row(fields, number))
    return rows


def _stat_row(fields: dict[str, str], number: int) -> StatRow:
    """Build a row from its columns. `number` is the line number for error messages."""
    try:
        row_type = ProxyType(int(fields["type"]))
    except ValueError:
        raise StatsParseError(f"show stat line {number} has an unknown type") from None

    def counter(name: str) -> int | None:
        value = fields.get(name, "")
        if not value:
            return None
        try:
            return int(value)
        except ValueError:
            raise StatsParseError(f"show stat line {number}: {name} is not a number") from None

    return StatRow(
        proxy=fields["pxname"],
        service=fields["svname"],
        type=row_type,
        status=fields.get("status", ""),
        responses_5xx=counter("hrsp_5xx"),
        requests_total=counter("req_tot"),
        check_status=fields.get("check_status", ""),
    )


def _required_int(fields: Mapping[str, str], name: str) -> int:
    """Return a required numeric field of `show info`."""
    try:
        return int(fields[name])
    except (KeyError, ValueError):
        raise StatsParseError(f"show info output has no valid {name}") from None
