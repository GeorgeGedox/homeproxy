"""The DDNS job: point each domain's DDNS record at the WAN address.

It runs at start, every 5 minutes, after every successful apply, on "Reconcile now" and after an
adoption.
"""

import logging
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

from homeproxy.cloudflare.ddns import WAN_IP_URL, DdnsResult, update_ddns
from homeproxy.jobs.runner import JobRunner
from homeproxy.jobs.tracker import error_text
from homeproxy.notify import mark_delivered, notify
from homeproxy.store import Store

logger = logging.getLogger(__name__)

KIND = "cloudflare.ddns"
INTERVAL = timedelta(minutes=5)
FAILING_FILE = Path("state") / "ddns-failing"
"""Present while DDNS fails, so a restart does not send the `ddns_failed` message again."""


class Ddns:
    """Runs the DDNS update on `/data/config` and keeps the last results in `results`, and when
    they were checked in `checked_at`.

    A failed run raises, so the job runner logs it, and `results` and `checked_at` keep the
    previous run. The Cloudflare page shows `results`: the address of each record, and which ones
    need adoption. One run at a time, so two runs never both create the record.

    A run that fails, or fails for a zone, sends a `ddns_failed` notification when the run before
    it worked. One message is sent until DDNS works again, not one every 5 minutes or per restart.
    """

    def __init__(self, store: Store, wan_url: str = WAN_IP_URL) -> None:
        self._store = store
        self._wan_url = wan_url
        self._lock = threading.Lock()
        self.results: list[DdnsResult] = []
        self.checked_at: datetime | None = None

    def run(self) -> None:
        """Update once, store the results and notify when DDNS starts failing."""
        try:
            with self._lock:
                self._update()
                self.checked_at = datetime.now(UTC)
        except Exception as error:
            self._report(error_text(error))
            raise
        failed = [f"{r.zone}: {r.error}" for r in self.results if r.action == "failed"]
        self._report("; ".join(failed))

    def _report(self, failure: str) -> None:
        path = self._store.root / FAILING_FILE
        try:
            if not failure:
                path.unlink(missing_ok=True)
                return
            if path.exists():
                return
            # Marked only once a target took it, so a message lost to an outage is sent again.
            # shortcut: a success while this send is still retrying is undone when it lands, and the
            # next run of failures stays silent; track the run in the marker if that is ever seen.
            notify(
                self._store,
                "ddns_failed",
                "DDNS update failed",
                failure,
                delivered=lambda: mark_delivered(path),
            )
        except OSError as error:
            logger.error("cannot record the DDNS state: %s", error)

    def _update(self) -> None:
        if not self._store.configured():
            self.results = []
            return
        config = self._store.load_config()
        if not config.cloudflare.enabled:
            self.results = []  # secrets.yaml may not exist yet, so it is not read
            return
        self.results = update_ddns(config, self._store.load_secrets(), wan_url=self._wan_url)

    def submit(self, runner: JobRunner) -> None:
        """Queue one tracked run."""
        runner.submit(KIND, self.run)

    def schedule(self, runner: JobRunner) -> None:
        """Run now, then every 5 minutes without filling the job history."""
        self.submit(runner)
        runner.schedule_every(KIND, self.run, interval=INTERVAL, track=False)
