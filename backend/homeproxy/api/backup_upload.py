"""What the routes that take a backup archive share: the raw upload, the staging directory, the
refusals, the check answer and the restart after a restore.

The body is the raw archive (`application/gzip`), streamed to a staging directory in `/data`. The
archive is hostile input; see `homeproxy.backup.extract`.
"""

import logging
import os
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import Request, Response
from pydantic import BaseModel
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from homeproxy.api.apply import Services, apply
from homeproxy.api.errors import ApiError
from homeproxy.auth.runtime import AuthRuntime
from homeproxy.backup import UPLOAD_NAME, BackupSummary, RestoreRefused, staging_dir
from homeproxy.backup.layout import MAX_UPLOAD_BYTES

logger = logging.getLogger(__name__)

ARCHIVE_BODY: dict[str, Any] = {
    "requestBody": {
        "required": True,
        "content": {"application/gzip": {"schema": {"type": "string", "format": "binary"}}},
    }
}
"""`openapi_extra` of a route that reads the archive from the raw body."""

UPLOAD_RESPONSES: dict[int | str, dict[str, Any]] = {
    413: {"description": "The upload, the extracted data or the file count is over the cap"},
    422: {
        "description": "Not a HomeProxy backup, a refused member, a newer file version, or a "
        "file that does not validate; the message names the file"
    },
}


class BackupCheck(BaseModel):
    """What a backup holds. It has no secret value, only whether a secret is set.

    `made_at` is the newest member time in the archive. `config_version` is the file version in the
    backup and `migrated` says that a restore updates the files from an older HomeProxy.
    """

    made_at: datetime | None
    config_version: int
    migrated: bool
    routes: int
    domains: list[str]
    certificates: int
    snapshots: int
    password_set: bool
    backup_token_set: bool

    @classmethod
    def of(cls, summary: BackupSummary) -> "BackupCheck":
        """Map the package's summary to the answer."""
        return cls(**vars(summary))


def _too_large() -> ApiError:
    return ApiError(413, "backup_too_large", "The backup file is too large.")


async def _save_upload(request: Request, path: str) -> None:
    """Write the request body to `path`. Raises a 413 `ApiError` over `MAX_UPLOAD_BYTES`."""
    length = request.headers.get("content-length", "")
    if length.isdecimal() and int(length) > MAX_UPLOAD_BYTES:
        raise _too_large()
    size = 0
    with open(path, "wb") as handle:
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                raise _too_large()
            await run_in_threadpool(handle.write, chunk)
        await run_in_threadpool(os.fsync, handle.fileno())


async def with_upload[T](request: Request, root: Path, work: Callable[[Path], T]) -> T:
    """Save the body into a staging directory under `root`, run `work(staging)` in a thread and
    remove the directory. A `RestoreRefused` of the work becomes an `ApiError`."""
    with staging_dir(root) as staging:
        await _save_upload(request, str(staging / UPLOAD_NAME))
        try:
            return await run_in_threadpool(work, staging)
        except RestoreRefused as refusal:
            raise ApiError(refusal.status, refusal.code, refusal.message) from None


def stop_dashboard(runtime: AuthRuntime) -> None:
    """Ask the dashboard server to stop, so that its supervisor starts it with the restored
    certificate. Without a server, as in a test, nothing happens."""
    server = runtime.listen_change.serving
    if server is not None:
        logger.info("restarting the dashboard to load the restored certificate")
        server.should_exit = True


async def finish_restore(runtime: AuthRuntime, services: Services, peer: str) -> Response:
    """Audit a restore whose files have moved, run the first apply and restart the dashboard.

    The apply cannot fail the restore, so it is caught whatever it raises: its failure shows on
    Overview, and the admin fixes the value in Settings.
    """
    runtime.store.audit.record("admin", "backup.restore", {"address": peer})
    try:
        await run_in_threadpool(apply, services)
    except Exception as error:  # the files moved, so the restart must happen
        logger.error("the apply after a restore could not run: %s", type(error).__name__)
    return Response(status_code=204, background=BackgroundTask(stop_dashboard, runtime))
