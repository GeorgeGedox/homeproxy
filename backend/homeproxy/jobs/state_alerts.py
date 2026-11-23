"""State alerts: a route or the CrowdSec bouncer down and recovered, and a certificate that expires
within 7 days.

One message per change of state, never one per check. The states already sent are kept in
`/data/state/alerts.json`, so a restart does not send them again.
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, ValidationError

from homeproxy.api.status import bouncer_state
from homeproxy.certs.bundle import ALERT_DAYS, CertificateInfo
from homeproxy.generator import BACKEND_PREFIX
from homeproxy.haproxy.client import HAProxyError
from homeproxy.haproxy.stats import ProxyType, StatRow
from homeproxy.jobs.runner import JobRunner
from homeproxy.notify import notify
from homeproxy.store import Store, StoreError
from homeproxy.util.atomic import atomic_write, ensure_dir

logger = logging.getLogger(__name__)

KIND = "notify.state-alerts"
TICK = timedelta(seconds=15)
DOWN_AFTER = timedelta(minutes=2)
"""How long a route or the bouncer stays down before the message, so a restart of it is quiet."""
EXPIRING_WITHIN = timedelta(days=ALERT_DAYS)
STATE_FILE = Path("state") / "alerts.json"
BOUNCER = "bouncer"
ROUTE = "route:"
"""Prefix of a route's key in `SentAlerts.down`."""


class SentAlerts(BaseModel):
    """What was sent last: the keys reported down, and each expiring certificate's expiry."""

    down: set[str] = set()
    expiring: dict[str, datetime] = {}


def _utcnow() -> datetime:
    return datetime.now(UTC)


class StateAlerts:
    """Sends `route_down`, `bouncer_down` and `certificate_expiring` on each change of state.

    `stats` is `client.show_stat` and `certificates` lists the installed certificates. A route is
    watched while it is enabled, so a route without a check path is watched through HAProxy's TCP
    check. It is down while its HAProxy backend
    has no healthy server, and the bouncer while HAProxy's check of it fails. Either is reported
    after `down_after` on `clock`, and its recovery at once. A certificate is reported once per
    expiry date, so once per renewal.
    """

    def __init__(
        self,
        store: Store,
        stats: Callable[[], list[StatRow]],
        certificates: Callable[[], list[CertificateInfo]],
        *,
        down_after: timedelta = DOWN_AFTER,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._store = store
        self._stats = stats
        self._certificates = certificates
        self._down_after = down_after
        self._clock = clock
        self._down_since: dict[str, datetime] = {}
        self._problem = ""
        self._sent = self._load()

    def run(self) -> None:
        """Check once, send a message for each change, and save what was sent."""
        now = self._clock()
        before = self._sent.model_copy(deep=True)
        problem = self._check_health(now)
        self._check_certificates(now)
        if self._sent != before:
            self._save()
        self._report(problem)

    def _check_health(self, now: datetime) -> str:
        """Update the route and bouncer states, and return why they could not be read, or `""`."""
        try:
            watched = self._watched()
        except (OSError, StoreError) as error:
            return f"cannot read the routes: {error}"
        except HAProxyError as error:
            return f"HAProxy does not answer, so health is not checked: {error}"
        # A route that was disabled or removed leaves without a message.
        self._sent.down &= watched.keys()
        self._down_since = {k: v for k, v in self._down_since.items() if k in watched}
        for key, down in watched.items():
            self._update(key, down, now)
        return ""

    def _watched(self) -> dict[str, bool]:
        """Return whether each watched route, and the bouncer, is down, as `show stat` has them."""
        # HAProxy checks every server, with a TCP connect where the route has no check path.
        checked = {ROUTE + route.name for route in self._store.load_routes() if route.enabled}
        rows = self._stats()
        watched = {
            ROUTE + row.proxy.removeprefix(BACKEND_PREFIX): not row.status.startswith("UP")
            for row in rows
            if row.type is ProxyType.BACKEND and row.proxy.startswith(BACKEND_PREFIX)
        }
        watched = {key: down for key, down in watched.items() if key in checked}
        bouncer = bouncer_state(rows)
        if bouncer is not None:
            watched[BOUNCER] = bouncer == "down"
        return watched

    def _update(self, key: str, down: bool, now: datetime) -> None:
        if not down:
            self._down_since.pop(key, None)
            if key in self._sent.down:
                self._sent.down.discard(key)
                self._notify_recovered(key)
            return
        since = self._down_since.setdefault(key, now)
        if key not in self._sent.down and now - since >= self._down_after:
            self._sent.down.add(key)
            self._notify_down(key, since)

    def _notify_down(self, key: str, since: datetime) -> None:
        when = f"{since:%Y-%m-%d %H:%M} UTC"
        if key == BOUNCER:
            text = f"HAProxy has not reached the bouncer since {when}. Requests pass unchecked."
            notify(self._store, "bouncer_down", "CrowdSec bouncer is down", text)
            return
        name = key.removeprefix(ROUTE)
        text = f"HAProxy has found no healthy server since {when}."
        notify(self._store, "route_down", f"Route {name} is down", text)

    def _notify_recovered(self, key: str) -> None:
        if key == BOUNCER:
            text = "HAProxy reaches the bouncer again."
            notify(self._store, "bouncer_down", "CrowdSec bouncer recovered", text, recovered=True)
            return
        name = key.removeprefix(ROUTE)
        text = "HAProxy reaches a healthy server again."
        notify(self._store, "route_down", f"Route {name} recovered", text, recovered=True)

    def _check_certificates(self, now: datetime) -> None:
        expiring = {
            cert.name: cert.not_after
            for cert in self._certificates()
            if cert.not_after - now <= EXPIRING_WITHIN
        }
        for name, not_after in expiring.items():
            if self._sent.expiring.get(name) == not_after:
                continue
            text = (
                f"It expires on {not_after:%Y-%m-%d %H:%M} UTC. Renew it on the Certificates page."
            )
            notify(self._store, "certificate_expiring", f"Certificate {name} expires soon", text)
        self._sent.expiring = expiring

    def _load(self) -> SentAlerts:
        path = self._store.root / STATE_FILE
        try:
            return SentAlerts.model_validate_json(path.read_bytes())
        except FileNotFoundError:
            return SentAlerts()
        except (OSError, ValidationError) as error:
            logger.error("cannot read the sent alerts, so they may be sent again: %s", error)
            return SentAlerts()

    def _save(self) -> None:
        path = self._store.root / STATE_FILE
        try:
            ensure_dir(path.parent)
            atomic_write(path, self._sent.model_dump_json().encode())
        except OSError as error:
            logger.error(
                "cannot record the sent alerts, so a restart may send them again: %s", error
            )

    def _report(self, problem: str) -> None:
        """Log a problem once when it starts, and once when it ends."""
        if problem == self._problem:
            return
        if problem:
            logger.warning("state alerts: %s", problem)
        else:
            logger.info("state alerts: health is checked again")
        self._problem = problem

    def schedule(self, runner: JobRunner) -> None:
        """Check every `TICK` without filling the job history."""
        runner.schedule_every(KIND, self.run, interval=TICK, track=False)
