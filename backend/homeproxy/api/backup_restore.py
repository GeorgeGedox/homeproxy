"""`POST /setup/restore` and `/setup/restore/check`: restore a backup on a fresh install, in place
of setup, or describe it first.

They use the setup claim: the one-time setup token in the `X-Setup-Token` header and a private
client address. The body is the raw archive (`application/gzip`), streamed to a staging directory
in `/data` (see `api.backup_upload`). A refusal before the files move leaves `/data` as it was,
and the same setup token works again. A check never uses the token up.

After the files move, the first apply runs, as after setup. No failure of it fails the restore or
skips the restart: the dashboard stops, so that the service supervisor starts it again with the
restored `tls/` certificate.
"""

import os
from typing import Annotated, Any

from fastapi import APIRouter, Header, Request, Response

from homeproxy.api.apply import ServicesDep
from homeproxy.api.backup_upload import (
    ARCHIVE_BODY,
    UPLOAD_RESPONSES,
    BackupCheck,
    finish_restore,
    with_upload,
)
from homeproxy.auth.routes import ClientAddress, Runtime
from homeproxy.auth.server import bootstrap_listen
from homeproxy.backup import check_backup, restore_backup

router = APIRouter(prefix="/api/v1", tags=["setup"])

SetupTokenHeader = Annotated[str, Header(alias="X-Setup-Token")]

CLAIM_RESPONSES: dict[int | str, dict[str, Any]] = {
    403: {"description": "Wrong setup token, or the client is not on a private address"},
    409: {"description": "Setup has already run"},
    429: {"description": "Too many wrong tokens; see Retry-After"},
}


@router.post(
    "/setup/restore/check",
    response_model=BackupCheck,
    openapi_extra=ARCHIVE_BODY,
    responses={**CLAIM_RESPONSES, **UPLOAD_RESPONSES},
)
async def check(
    request: Request, token: SetupTokenHeader, runtime: Runtime, peer: ClientAddress
) -> BackupCheck:
    """Describe a backup, as `POST /setup/restore` would take it, and change nothing.

    The same claim checks as the restore, but the setup token stays usable. A wrong token counts
    toward the throttle. The body is the raw `application/gzip` archive.
    """
    with runtime.setup_claim(peer, token, consume=False):
        summary = await with_upload(request, runtime.store.root, check_backup)
    return BackupCheck.of(summary)


@router.post(
    "/setup/restore",
    status_code=204,
    response_class=Response,
    openapi_extra=ARCHIVE_BODY,
    responses={**CLAIM_RESPONSES, **UPLOAD_RESPONSES},
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
        listen = f"{host}:{port}"
        await with_upload(
            request,
            runtime.store.root,
            lambda staging: restore_backup(runtime.store, staging, listen),
        )
    return await finish_restore(runtime, services, peer)
