"""The heartbeat job: GET the ping URL every interval, only while HAProxy answers on its socket.

An outside service such as healthchecks.io alerts when the pings stop, so it reports what HomeProxy
cannot report itself: the host, the container or HAProxy is down. The ping URL is a secret and is
never logged.
"""

import logging
import time
from collections.abc import Callable
from datetime import timedelta

from homeproxy.haproxy.client import HAProxyError
from homeproxy.jobs.runner import JobRunner
from homeproxy.notify import NotifyError, send_once
from homeproxy.store import Store, StoreError

logger = logging.getLogger(__name__)

KIND = "notify.heartbeat"
TICK = timedelta(seconds=5)
"""How often the job wakes up. It reads the interval each time, so a change applies at once."""


class Heartbeat:
    """Pings `secrets.yaml` `notifications.heartbeat_url` while the heartbeat is on.

    `probe` asks HAProxy for `show info` and raises `HAProxyError` when it does not answer. A ping
    happens when the interval has passed since the last one, measured on `clock`. While HAProxy does
    not answer there is no ping, and the first tick after it answers again pings at once.

    One timeout of `TIMEOUT` seconds and no retry: the next interval is the retry. A problem is
    logged once when it starts and once when the pings work again, never with the URL.
    """

    def __init__(
        self,
        store: Store,
        probe: Callable[[], object],
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store = store
        self._probe = probe
        self._clock = clock
        self._last: float | None = None
        self._problem = ""

    def run(self) -> None:
        """Ping once if the heartbeat is on, the interval has passed and HAProxy answers."""
        if not self._store.configured():
            return
        try:
            heartbeat = self._store.load_config().notifications.heartbeat
        except (OSError, StoreError) as error:
            self._report(f"cannot read config.yaml: {error}")
            return
        if not heartbeat.enabled:
            self._last = None
            return
        now = self._clock()
        # Half a tick early, so a tick that wakes a few milliseconds early does not skip a ping.
        due = heartbeat.seconds - TICK.total_seconds() / 2
        if self._last is not None and now - self._last < due:
            return
        try:
            self._probe()
        except HAProxyError:
            self._report("HAProxy does not answer on its socket, so no ping is sent")
            return
        self._last = now
        self._report(self._ping())

    def _ping(self) -> str:
        """Send the ping and return what went wrong, or `""`."""
        try:
            url = self._store.load_secrets().notifications.heartbeat_url.get_secret_value()
        except (OSError, StoreError) as error:
            return f"cannot read secrets.yaml: {error}"
        if not url:
            return "the ping URL is not set"
        try:
            status = send_once(url)
        except NotifyError as error:
            return str(error)
        return "" if 200 <= status < 300 else f"HTTP {status}"

    def _report(self, problem: str) -> None:
        if problem == self._problem:
            return
        if problem:
            logger.warning("heartbeat: %s", problem)
        else:
            logger.info("heartbeat: pings arrive again")
        self._problem = problem

    def schedule(self, runner: JobRunner) -> None:
        """Wake up every `TICK` without filling the job history."""
        runner.schedule_every(KIND, self.run, interval=TICK, track=False)
