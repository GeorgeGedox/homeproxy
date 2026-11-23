"""The daily HAProxy version check, and the last result it found for the status API."""

import logging
import re
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

from homeproxy.haproxy.version_check import RELEASES_URL, VersionStatus, check_version
from homeproxy.jobs.runner import JobRunner
from homeproxy.notify import mark_delivered, notify
from homeproxy.store import Store

logger = logging.getLogger(__name__)

KIND = "haproxy.version-check"
INTERVAL = timedelta(days=1)
NOTIFIED_FILE = Path("state") / "notified-version"
"""The newest version a target got a `new_version` message for, so a restart does not repeat it.
It is written only after a target accepted the message, so a target added later still hears of the
current version."""

_RELEASE = re.compile(r"[0-9]{1,4}\.[0-9]{1,4}(\.[0-9]{1,4})?")


class VersionCheck:
    """Runs the version check and keeps its last successful result in `status`.

    `running_version` returns the version HAProxy reports, for example
    `lambda: client.show_info().version`. A failed run raises, so the job runner logs it and
    marks the job failed, and `status` keeps the previous result. HAProxy is never touched.

    With a `store`, a newer version sends one `new_version` notification.
    """

    def __init__(
        self,
        running_version: Callable[[], str],
        url: str = RELEASES_URL,
        *,
        store: Store | None = None,
    ) -> None:
        self._running_version = running_version
        self._url = url
        self._store = store
        self.status: VersionStatus | None = None

    def run(self) -> None:
        """Check once, store the result in `status` and notify of a version not notified yet."""
        self.status = check_version(self._running_version(), self._url)
        if self.status.update_available and self._store is not None:
            self._notify(self._store, self.status)

    def _notify(self, store: Store, status: VersionStatus) -> None:
        if _RELEASE.fullmatch(status.latest) is None:
            # The name comes from endoflife.date and goes into a chat message.
            logger.warning("not notifying of a release with an unexpected name")
            return
        path = store.root / NOTIFIED_FILE
        try:
            if path.exists() and path.read_text() == status.latest:
                return
        except OSError as error:
            logger.error("cannot read the notified HAProxy version: %s", error)
            return
        notify(
            store,
            "new_version",
            f"HAProxy {status.latest} is available",
            f"HomeProxy runs HAProxy {status.running}. Update the HomeProxy image to get it.",
            delivered=lambda: mark_delivered(path, status.latest.encode()),
        )

    def schedule(self, runner: JobRunner) -> None:
        """Run the check now, then once a day."""
        runner.submit(KIND, self.run)
        runner.schedule_every(KIND, self.run, interval=INTERVAL)
