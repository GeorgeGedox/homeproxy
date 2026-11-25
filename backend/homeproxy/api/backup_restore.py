"""`POST /setup/restore`: restore a backup on a fresh install, in place of setup.

It uses the setup claim: the one-time setup token in the `X-Setup-Token` header and a private
client address. The body is the raw archive (`application/gzip`), streamed to a staging directory
in `/data`. The archive is hostile input; see `homeproxy.backup.extract`. A refusal before the
files move leaves `/data` as it was, and the same setup token works again.

After the files move, the first apply runs, as after setup, and its failure does not fail the
restore. Then the dashboard stops, so that the service supervisor starts it again with the restored
`tls/` certificate.
"""

import logging
import os
from typing import Annotated

from fastapi import APIRouter, Header, Request, Response
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from homeproxy.api.apply import ServicesDep, apply
from homeproxy.api.errors import ApiError
from homeproxy.auth.routes import ClientAddress, Runtime
from homeproxy.auth.runtime import AuthRuntime
from homeproxy.auth.server import bootstrap_listen
from homeproxy.backup import UPLOAD_NAME, RestoreRefused, restore_backup, staging_dir
from homeproxy.backup.layout import MAX_UPLOAD_BYTES
from homeproxy.store import StoreError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["setup"])

SetupTokenHeader = Annotated[str, Header(alias="X-Setup-Token")]


def _too_large() -> ApiError:
    return ApiError(413, "backup_too_large", "The backup file is too large.")


def stop_dashboard(runtime: AuthRuntime) -> None:
    """Ask the dashboard server to stop, so that its supervisor starts it with the restored
    certificate. Without a server, as in a test, nothing happens."""
    server = runtime.listen_change.serving
    if server is not None:
        logger.info("restarting the dashboard to load the restored certificate")
        server.should_exit = True


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


@router.post(
    "/setup/restore",
    status_code=204,
    response_class=Response,
    responses={
        403: {"description": "Wrong setup token, or the client is not on a private address"},
        409: {"description": "Setup has already run"},
        413: {"description": "The upload, the extracted data or the file count is over the cap"},
        422: {
            "description": "Not a HomeProxy backup, a refused member, a newer file version, or a "
            "file that does not validate; the message names the file"
        },
        429: {"description": "Too many wrong tokens; see Retry-After"},
    },
)
async def restore(
    request: Request,
    token: SetupTokenHeader,
    runtime: Runtime,
    peer: ClientAddress,
    services: ServicesDep,
) -> Response:
    """Restore a backup made by `GET /backup`. The body is the raw `application/gzip` archive.

    Needs no session. Afterwards the password, the routes and the certificates are the backup's,
    and `dashboard.listen` is the address the dashboard is on now. The dashboard restarts.
    """
    with runtime.setup_claim(peer, token):
        host, port = bootstrap_listen(os.environ)
        with staging_dir(runtime.store.root) as staging:
            await _save_upload(request, str(staging / UPLOAD_NAME))
            try:
                await run_in_threadpool(restore_backup, runtime.store, staging, f"{host}:{port}")
            except RestoreRefused as refusal:
                raise ApiError(refusal.status, refusal.code, refusal.message) from None
    runtime.store.audit.record("admin", "backup.restore", {"address": peer})
    try:
        await run_in_threadpool(apply, services)
    except (OSError, StoreError) as error:
        # As in setup: the failure shows on Overview, and the admin fixes the value in Settings.
        logger.error("the first apply after a restore could not run: %s", error)
    return Response(status_code=204, background=BackgroundTask(stop_dashboard, runtime))
