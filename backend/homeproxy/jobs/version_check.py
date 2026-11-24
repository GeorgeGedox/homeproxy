"""The daily HAProxy version check, and the last result it found for the status API."""

from collections.abc import Callable
from datetime import timedelta

from homeproxy.haproxy.version_check import RELEASES_URL, VersionStatus, check_version
from homeproxy.jobs.runner import JobRunner

KIND = "haproxy.version-check"
INTERVAL = timedelta(days=1)


class VersionCheck:
    """Runs the version check and keeps its last successful result in `status`.

    `running_version` returns the version HAProxy reports, for example
    `lambda: client.show_info().version`. A failed run raises, so the job runner logs it and
    marks the job failed, and `status` keeps the previous result. HAProxy is never touched.

    It sends no notification: only a HomeProxy release can bring a newer HAProxy, and the
    HomeProxy update check reports that.
    """

    def __init__(self, running_version: Callable[[], str], url: str = RELEASES_URL) -> None:
        self._running_version = running_version
        self._url = url
        self.status: VersionStatus | None = None

    def run(self) -> None:
        """Check once and store the result in `status`."""
        self.status = check_version(self._running_version(), self._url)

    def schedule(self, runner: JobRunner) -> None:
        """Run the check now, then once a day."""
        runner.submit(KIND, self.run)
        runner.schedule_every(KIND, self.run, interval=INTERVAL)
