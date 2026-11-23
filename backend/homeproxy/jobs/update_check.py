"""The daily HomeProxy update check: the running version against the newest published release.

The source is a GitHub releases "latest" JSON document, `{"tag_name": "v0.2.0", ...}`. Release
tags `vX.Y.Z` publish the image, so a newer tag means "update the HomeProxy image".
"""

import http.client
import importlib.metadata
import json
import logging
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from homeproxy.jobs.runner import JobRunner
from homeproxy.notify import mark_delivered, notify
from homeproxy.store import Store
from homeproxy.util import http as http_request

logger = logging.getLogger(__name__)

UPDATE_URL: str | None = None
"""The release source. None while the repository is private: then nothing is fetched."""
UPDATE_URL_ENV = "HOMEPROXY_UPDATE_URL"
"""Replaces `UPDATE_URL` when set. Only tests and the dev stack set it, to a fake release."""
KIND = "homeproxy.update-check"
INTERVAL = timedelta(days=1)
FETCH_TIMEOUT = 15.0
NOTIFIED_FILE = Path("state") / "notified-homeproxy-version"
"""The newest version a target got a `new_version` message for, as for the HAProxy check."""

# The tag goes into a chat message, so only a plain `vX.Y.Z` is accepted.
_VERSION = re.compile(r"v?([0-9]{1,4})\.([0-9]{1,4})\.([0-9]{1,4})")


def update_url() -> str | None:
    """Return `UPDATE_URL_ENV` if it is set, or `UPDATE_URL`."""
    return os.environ.get(UPDATE_URL_ENV) or UPDATE_URL


class UpdateCheckError(Exception):
    """The release could not be fetched or read, or a version is not `X.Y.Z`."""


class _Release(BaseModel):
    tag_name: str


class UpdateStatus(BaseModel):
    """The outcome of one HomeProxy update check, as the status API reports it."""

    model_config = ConfigDict(frozen=True)

    running: str = Field(description="HomeProxy version from the package metadata")
    latest: str = Field(description="Version of the newest release, without the `v`")
    update_available: bool = Field(description="True when `latest` is newer than `running`")
    checked_at: datetime


def _version_key(version: str) -> tuple[int, int, int]:
    match = _VERSION.fullmatch(version)
    if match is None:
        raise UpdateCheckError(f"not a HomeProxy version: {version[:40]!r}")
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch)


def _fetch_latest(url: str) -> str:
    """Return the tag of the newest release."""
    try:
        with http_request.urlopen(url, timeout=FETCH_TIMEOUT) as response:
            body = response.read()
        return _Release.model_validate(json.loads(body)).tag_name
    except (OSError, ValueError, http.client.HTTPException) as exc:
        # URLError and timeouts are OSErrors, bad JSON and ValidationError are ValueErrors, and
        # an answer cut short is an HTTPException.
        raise UpdateCheckError(f"cannot read the newest HomeProxy release: {exc}") from None


def check_update(running: str, url: str) -> UpdateStatus:
    """Compare `running` with the newest release at `url`."""
    latest = _version_key(_fetch_latest(url))
    return UpdateStatus(
        running=running,
        latest=".".join(map(str, latest)),
        update_available=latest > _version_key(running),
        checked_at=datetime.now(UTC),
    )


class UpdateCheck:
    """Runs the update check and keeps its last successful result in `status`.

    With `url` None the check is not configured: `schedule` submits nothing. A failed run raises,
    so the job runner logs it, and `status` keeps the previous result. With a `store`, a newer
    version sends one `new_version` notification.
    """

    def __init__(self, url: str | None, *, store: Store | None = None) -> None:
        self.url = url
        self._store = store
        self.status: UpdateStatus | None = None

    def run(self) -> None:
        """Check once, store the result in `status` and notify of a version not notified yet."""
        if self.url is None:
            return
        self.status = check_update(importlib.metadata.version("homeproxy"), self.url)
        if self.status.update_available and self._store is not None:
            self._notify(self._store, self.status)

    def _notify(self, store: Store, status: UpdateStatus) -> None:
        path = store.root / NOTIFIED_FILE
        try:
            if path.exists() and path.read_text() == status.latest:
                return
        except OSError as error:
            logger.error("cannot read the notified HomeProxy version: %s", error)
            return
        notify(
            store,
            "new_version",
            f"HomeProxy {status.latest} is available",
            f"This is HomeProxy {status.running}. Update the HomeProxy image to get it.",
            delivered=lambda: mark_delivered(path, status.latest.encode()),
        )

    def schedule(self, runner: JobRunner) -> None:
        """Run the check now, then once a day. Nothing runs while no source is set."""
        if self.url is None:
            return
        runner.submit(KIND, self.run)
        runner.schedule_every(KIND, self.run, interval=INTERVAL)
