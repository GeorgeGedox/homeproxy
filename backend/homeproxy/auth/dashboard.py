"""Read the settings auth needs from `config.yaml`: the `dashboard` section and `lan_cidrs`.

Only these fields are validated, so a mistake elsewhere in a hand-edited file does not lock the
admin out of the dashboard that would show it. The file is read again whenever it changes on
disk, so `homeproxy set-password` takes effect without a restart.
"""

import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from pydantic import Field, TypeAdapter, ValidationError

from homeproxy.auth.sessions import parse_duration
from homeproxy.models import safe_errors
from homeproxy.models.config import Dashboard
from homeproxy.models.fields import IpOrCidr
from homeproxy.store.errors import StoreError
from homeproxy.store.tree import load_tree
from homeproxy.util.redact import register_secret

LanCidrs = Annotated[list[IpOrCidr], Field(min_length=1)]
_LAN_CIDRS: TypeAdapter[list[str]] = TypeAdapter(LanCidrs, config={"strict": True})


class DashboardConfigError(Exception):
    """The dashboard settings in `config.yaml` cannot be used. The text holds no file content."""


@dataclass(frozen=True)
class DashboardSettings:
    """What auth and the server need from `config.yaml`."""

    listen: str
    password_hash: str
    session_ttl: int
    lan_cidrs: tuple[str, ...]


def _describe(error: ValidationError, prefix: str) -> str:
    parts = []
    for item in safe_errors(error):
        location = ".".join(str(segment) for segment in (prefix, *item["loc"]))
        parts.append(f"{location}: {item['msg']}")
    return "; ".join(parts)


def read_dashboard_settings(path: Path) -> DashboardSettings:
    """Load and check the auth fields of `config.yaml`. Raise `DashboardConfigError` if unusable.

    A missing file raises `FileNotFoundError`. The password hash is registered for redaction.
    """
    try:
        tree = load_tree(path)
    except StoreError as error:
        raise DashboardConfigError(str(error)) from None
    if not isinstance(tree, Mapping):
        raise DashboardConfigError(f"{path.name}: the top level must be a mapping")
    failure = ""
    try:
        dashboard = Dashboard.model_validate(tree.get("dashboard"))
    except ValidationError as error:
        failure = _describe(error, "dashboard")
    if failure:
        raise DashboardConfigError(f"{path.name}: {failure}")
    network = tree.get("network")
    raw_cidrs = network.get("lan_cidrs") if isinstance(network, Mapping) else None
    try:
        lan_cidrs = _LAN_CIDRS.validate_python(raw_cidrs)
    except ValidationError as error:
        failure = _describe(error, "network.lan_cidrs")
    if failure:
        raise DashboardConfigError(f"{path.name}: {failure}")
    password_hash = dashboard.password_hash.get_secret_value()
    register_secret(password_hash)
    return DashboardSettings(
        listen=dashboard.listen,
        password_hash=password_hash,
        session_ttl=parse_duration(dashboard.session_ttl),
        lan_cidrs=tuple(lan_cidrs),
    )


type FileStamp = tuple[int, int, int, int]


def file_stamp(path: Path) -> FileStamp | None:
    """Return what changes when a file is replaced or edited, or None if it does not exist."""
    try:
        status = os.stat(path)
    except FileNotFoundError:
        return None
    return status.st_ino, status.st_size, status.st_mtime_ns, status.st_ctime_ns


class DashboardSource:
    """Cached `read_dashboard_settings`, read again when the file changes."""

    def __init__(self, path: Path) -> None:
        """Watch the `config.yaml` at `path`."""
        self.path = path
        self._lock = threading.Lock()
        self._stamp: FileStamp | None = None
        self._result: DashboardSettings | DashboardConfigError | None = None

    def get(self) -> DashboardSettings | None:
        """Return the settings, or None if `config.yaml` does not exist.

        Raises `DashboardConfigError` if the file exists but its auth fields are unusable.
        """
        with self._lock:
            stamp = file_stamp(self.path)
            if stamp is None:
                self._stamp, self._result = None, None
                return None
            if stamp != self._stamp or self._result is None:
                try:
                    self._result = read_dashboard_settings(self.path)
                except FileNotFoundError:
                    self._stamp, self._result = None, None
                    return None
                except DashboardConfigError as error:
                    self._result = error
                self._stamp = stamp
            if isinstance(self._result, DashboardConfigError):
                raise DashboardConfigError(str(self._result))
            return self._result
