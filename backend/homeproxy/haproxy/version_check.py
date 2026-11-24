"""Find the newest patch of the newest HAProxy LTS branch, to show beside the running version.

The release data comes from the endoflife.date API. HAProxy is bundled in the image, so a
newer patch reaches the user only with a HomeProxy release.
"""

import http.client
import json
import os
import re
from datetime import UTC, date, datetime

from pydantic import BaseModel, ConfigDict, Field

from homeproxy.util import http as http_request

RELEASES_URL = "https://endoflife.date/api/v1/products/haproxy/"
RELEASES_URL_ENV = "HOMEPROXY_RELEASES_URL"
"""Replaces `RELEASES_URL` when set. Tests set it, so they never call endoflife.date."""
FETCH_TIMEOUT = 15.0

# A branch name such as `3.4`.
_VERSION = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")


def releases_url() -> str:
    """Return `RELEASES_URL_ENV` if it is set, or `RELEASES_URL`."""
    return os.environ.get(RELEASES_URL_ENV, RELEASES_URL)


class VersionCheckError(Exception):
    """The release data could not be fetched or read, or a version is not valid."""


class _Latest(BaseModel):
    # The name comes from the internet and is shown on the Overview page.
    name: str = Field(pattern=r"^[0-9]{1,4}\.[0-9]{1,4}(\.[0-9]{1,4})?$")


class _Release(BaseModel):
    name: str
    is_lts: bool = Field(alias="isLts")
    eol_from: date | None = Field(alias="eolFrom")
    latest: _Latest


class _Result(BaseModel):
    releases: list[_Release]


class _Product(BaseModel):
    result: _Result


class VersionStatus(BaseModel):
    """The outcome of one version check, as the status API reports it."""

    model_config = ConfigDict(frozen=True)

    running: str = Field(description="HAProxy version from `show info`")
    branch: str = Field(description="Newest LTS branch, for example `3.4`")
    latest: str = Field(description="Newest patch release of that branch")
    eol: date | None = Field(description="End of life of that branch")
    checked_at: datetime


def _version_key(version: str) -> tuple[int, int, int]:
    """Return (major, minor, patch) for a HAProxy version. A missing patch counts as 0."""
    match = _VERSION.match(version)
    if match is None:
        raise VersionCheckError(f"not a HAProxy version: {version[:40]!r}")
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch or 0)


def _fetch_releases(url: str = RELEASES_URL) -> list[_Release]:
    """Fetch every HAProxy release branch from endoflife.date."""
    try:
        with http_request.urlopen(url, timeout=FETCH_TIMEOUT) as response:
            body = response.read()
        return _Product.model_validate(json.loads(body)).result.releases
    except (OSError, ValueError, http.client.HTTPException) as exc:
        # URLError and timeouts are OSErrors, bad JSON and ValidationError are ValueErrors, and
        # an answer cut short is an HTTPException.
        raise VersionCheckError(f"cannot read HAProxy releases from {url}: {exc}") from None


def check_version(running: str, url: str = RELEASES_URL) -> VersionStatus:
    """Return `running` with the newest patch of the newest LTS branch."""
    lts = [release for release in _fetch_releases(url) if release.is_lts]
    if not lts:
        raise VersionCheckError("endoflife.date lists no HAProxy LTS branch")
    newest = max(lts, key=lambda release: _version_key(release.name))
    return VersionStatus(
        running=running,
        branch=newest.name,
        latest=newest.latest.name,
        eol=newest.eol_from,
        checked_at=datetime.now(UTC),
    )
