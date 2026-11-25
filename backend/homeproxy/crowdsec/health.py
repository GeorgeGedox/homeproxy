"""CrowdSec as the dashboard can check it: AppSec answering, and the connection test in Settings.

The dashboard has the LAPI as a machine and the bouncer key. Neither shows whether the engine is
registered with the Central API, so the blocklist part reports what the LAPI holds.
"""

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal

from homeproxy.auth.dashboard import FileStamp, file_stamp
from homeproxy.crowdsec.lapi import BLOCKLIST_ORIGINS, LapiClient, LapiError
from homeproxy.generator.crowdsec import appsec_url
from homeproxy.models.config import Crowdsec
from homeproxy.models.secrets import SecretsCrowdsec
from homeproxy.store import Store
from homeproxy.util import http as http_request

log = logging.getLogger(__name__)

TIMEOUT: Final = 3.0
APPSEC_TTL: Final = 15.0
"""How long an AppSec reading holds while the settings do not change. The alert job reads every
15 s, so the dashboard's `/status` polls reuse its reading."""
_PROBE_ADDRESS = "192.0.2.1"
"""A documentation address that AppSec checks as the client. It never matches a real one."""

type Part = Literal["machine", "bouncer", "appsec", "blocklist"]


class HealthError(Exception):
    """A part of CrowdSec does not work. The message is safe to show: it holds no key."""


@dataclass(frozen=True)
class CheckResult:
    """One line of the connection test. `ok` is None for a part that is off."""

    part: Part
    ok: bool | None
    message: str


def _get(url: str, headers: dict[str, str]) -> tuple[int, bytes]:
    """Return the status and body of a GET. Raises `HealthError` when nothing answers."""
    request = urllib.request.Request(url, headers=headers)  # noqa: S310 - URL from the config
    try:
        with http_request.urlopen(request, timeout=TIMEOUT) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        with error:
            return error.code, b""
    except OSError as error:
        log.info("crowdsec check %s: %s", urllib.parse.urlsplit(url).path, type(error).__name__)
        raise HealthError("does not answer, check the URL and that CrowdSec is running") from None


def probe_appsec(crowdsec: Crowdsec, bouncer_key: str) -> None:
    """Ask AppSec about a harmless request, as the bouncer does. Raises `HealthError`."""
    headers = {
        "X-Crowdsec-Appsec-Api-Key": bouncer_key,
        "X-Crowdsec-Appsec-Ip": _PROBE_ADDRESS,
        "X-Crowdsec-Appsec-Uri": "/",
        "X-Crowdsec-Appsec-Host": "homeproxy.invalid",
        "X-Crowdsec-Appsec-Verb": "GET",
    }
    status, body = _get(appsec_url(crowdsec), headers)
    if status == 401:
        raise HealthError("refuses the bouncer key")
    try:
        allowed = status == 200 and json.loads(body).get("action") == "allow"
    except (ValueError, AttributeError):
        allowed = False
    if not allowed:
        raise HealthError(f"answered HTTP {status} instead of allowing a harmless request")


def appsec_state(store: Store) -> Literal["up", "down"] | None:
    """Return whether AppSec checks requests for the bouncer with the saved settings, or None while
    it is off. Raises `StoreError` or `OSError` when the settings cannot be read."""
    crowdsec = store.load_config().crowdsec
    if not (crowdsec.enabled and crowdsec.appsec):
        return None
    try:
        probe_appsec(crowdsec, store.load_secrets().crowdsec.bouncer_key.get_secret_value())
    except HealthError:
        return "down"
    return "up"


class AppsecReading:
    """`appsec_state`, kept for `APPSEC_TTL` or until `config.yaml` or `secrets.yaml` change, so
    `/status` polls do not each wait up to `TIMEOUT` on AppSec."""

    def __init__(self, store: Store, clock: Callable[[], float] = time.monotonic) -> None:
        self._store = store
        self._clock = clock
        self._read: tuple[float, FileStamp | None, FileStamp | None] | None = None
        self._state: Literal["up", "down"] | None = None

    def __call__(self) -> Literal["up", "down"] | None:
        """Return AppSec's state, probing it again when the reading is old or the settings
        changed. Raises `StoreError` or `OSError` when the settings cannot be read."""
        now = self._clock()
        stamps = (file_stamp(self._store.config_path), file_stamp(self._store.secrets_path))
        if self._read is None or self._read[1:] != stamps or now - self._read[0] >= APPSEC_TTL:
            self._state = appsec_state(self._store)
            self._read = (now, *stamps)
        return self._state


def check_bouncer_key(crowdsec: Crowdsec, bouncer_key: str) -> None:
    """Ask the LAPI for decisions as the bouncer does. Raises `HealthError`."""
    url = f"{str(crowdsec.lapi_url).rstrip('/')}/v1/decisions?ip={_PROBE_ADDRESS}"
    status, _ = _get(url, {"X-Api-Key": bouncer_key})
    if status == 403:
        raise HealthError("refuses the bouncer key")
    if status != 200:
        raise HealthError(f"answered HTTP {status}")


def blocklist(client: LapiClient) -> tuple[int, datetime | None]:
    """Return the active blocklist decisions and the time of the newest pull that brought one.
    Raises `LapiError`."""
    found = [d for d in client.decisions() if d.origin in BLOCKLIST_ORIGINS]
    return len(found), max((d.created_at for d in found), default=None)


def connection_check(crowdsec: Crowdsec, keys: SecretsCrowdsec) -> list[CheckResult]:
    """Check each part on its own, so a part that fails does not hide the others. `lapi_url` must
    be set."""
    client = LapiClient(
        str(crowdsec.lapi_url),
        keys.machine_id.get_secret_value(),
        keys.machine_password.get_secret_value(),
    )
    return [
        _machine(client),
        _bouncer(crowdsec, keys),
        _appsec(crowdsec, keys),
        _blocklist(client),
    ]


def _machine(client: LapiClient) -> CheckResult:
    try:
        client.login()
    except LapiError as error:
        return CheckResult("machine", False, f"The machine cannot log in: {error}.")
    return CheckResult("machine", True, "The machine logs in to the LAPI.")


def _bouncer(crowdsec: Crowdsec, keys: SecretsCrowdsec) -> CheckResult:
    try:
        check_bouncer_key(crowdsec, keys.bouncer_key.get_secret_value())
    except HealthError as error:
        return CheckResult("bouncer", False, f"The LAPI {error}.")
    return CheckResult("bouncer", True, "The LAPI accepts the bouncer key.")


def _appsec(crowdsec: Crowdsec, keys: SecretsCrowdsec) -> CheckResult:
    if not crowdsec.appsec:
        return CheckResult("appsec", None, "AppSec is off.")
    try:
        probe_appsec(crowdsec, keys.bouncer_key.get_secret_value())
    except HealthError as error:
        return CheckResult("appsec", False, f"AppSec {error}.")
    return CheckResult("appsec", True, "AppSec checks requests for the bouncer.")


def _blocklist(client: LapiClient) -> CheckResult:
    try:
        count, updated_at = blocklist(client)
    except LapiError as error:
        return CheckResult("blocklist", False, f"The blocklists cannot be read: {error}.")
    if updated_at is None:
        return CheckResult("blocklist", True, "0 blocklist decisions are active.")
    when = f"{updated_at:%Y-%m-%d %H:%M} UTC"
    return CheckResult("blocklist", True, f"{count} blocklist decisions, last update {when}.")
