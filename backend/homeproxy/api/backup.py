"""`GET /backup`, the backup token endpoints, and the check and restore of a backup on an install
that is set up.

The download needs a session or `Authorization: Bearer <backup token>`. The middleware checks the
token on that path only and never makes a session from it. The archive holds secrets: it is built
into a private temp file in `state/` under the store lock, then streamed after the lock is released,
and deleted when the response ends. It never reaches a log. The token endpoints, the check and the
restore need a session. The restore takes everything from the archive, the password too, and ends
every session. `dashboard.listen` stays as it is now.
"""

import logging
import os
import tempfile
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from homeproxy.api.apply import ServicesDep
from homeproxy.api.backup_upload import (
    ARCHIVE_BODY,
    UPLOAD_RESPONSES,
    BackupCheck,
    finish_restore,
    with_upload,
)
from homeproxy.auth.middleware import BACKUP_TOKEN_STATE_KEY
from homeproxy.auth.routes import ClientAddress, CurrentSession, Runtime
from homeproxy.auth.tls import key_pin
from homeproxy.backup import archive_name, build_archive, check_backup, restore_backup
from homeproxy.backup.token import created_now, hash_token, new_token
from homeproxy.store.tree import load_tree
from homeproxy.util.atomic import ensure_dir

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["backup"])


class BackupTokenStatus(BaseModel):
    """Whether a backup token is set, and when it was made.

    `key_pin` is the pin of the dashboard certificate's public key, for `curl --pinnedpubkey
    "sha256//<pin>"`. It is null when the certificate cannot be read.
    """

    set: bool
    created_at: str | None = None
    key_pin: str | None = None


class NewBackupToken(BaseModel):
    """A new backup token. This is the only time it is shown."""

    token: str


def _auth_type(request: Request) -> Literal["session", "token"]:
    """Return how the request got in. The middleware lets in only a session or the token."""
    if getattr(request.state, BACKUP_TOKEN_STATE_KEY, False) is True:
        return "token"
    return "session"


@router.get(
    "/backup",
    response_class=FileResponse,
    response_model=None,
    responses={
        200: {
            "description": "The backup archive",
            "content": {"application/gzip": {"schema": {"type": "string", "format": "binary"}}},
        },
        401: {"description": "No valid session and no valid backup token"},
        429: {"description": "Too many wrong backup tokens; see Retry-After"},
    },
)
def download_backup(request: Request, runtime: Runtime, peer: ClientAddress) -> FileResponse:
    """Download `/data` as a `tar.gz`, without `generated`, `releases` and `state`.

    Needs a session or the backup token. The archive holds secrets and certificate keys.
    """
    store = runtime.store
    state = store.root / "state"
    ensure_dir(state)
    descriptor, name = tempfile.mkstemp(dir=state, prefix=".backup-", suffix=".tar.gz")
    os.close(descriptor)
    try:
        with store.lock() as held:
            build_archive(store, held, Path(name))
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise
    auth = _auth_type(request)
    store.audit.record(
        "admin" if auth == "session" else "backup-token",
        "backup.download",
        {"address": peer, "auth": auth},
    )
    logger.info("backup downloaded from %s with %s", peer, auth)
    return FileResponse(
        name,
        media_type="application/gzip",
        filename=archive_name(),
        headers={"Cache-Control": "no-store"},
        background=BackgroundTask(os.unlink, name),
    )


@router.get(
    "/backup/token",
    response_model=BackupTokenStatus,
    responses={401: {"description": "No valid session"}},
)
def backup_token_status(runtime: Runtime, session: CurrentSession) -> BackupTokenStatus:
    """Report whether a backup token is set, and when it was made. It never returns the token."""
    stored = runtime.store.load_secrets()
    try:
        pin = key_pin(runtime.store.root / "tls" / "dashboard.pem")
    except (OSError, ValueError, IndexError):
        pin = None
    return BackupTokenStatus(
        set=stored.backup_token_hash is not None,
        created_at=stored.backup_token_created_at,
        key_pin=pin,
    )


@router.post(
    "/backup/token",
    status_code=201,
    response_model=NewBackupToken,
    responses={
        401: {"description": "No valid session"},
        409: {"description": "secrets.yaml cannot be written"},
    },
)
def create_backup_token(
    runtime: Runtime, peer: ClientAddress, session: CurrentSession, response: Response
) -> NewBackupToken:
    """Make a backup token, or replace the one that is set. The old token stops working.

    The token is in the answer once. Only its SHA-256 is kept, in `secrets.yaml`.
    """
    token = new_token()
    store = runtime.store
    with store.lock() as held:
        tree = load_tree(store.secrets_path)
        tree["backup_token_hash"] = hash_token(token)
        tree["backup_token_created_at"] = created_now()
        store.save_secrets(tree, held)
    store.audit.record("admin", "backup.token_create", {"address": peer})
    response.headers["Cache-Control"] = "no-store"
    return NewBackupToken(token=token)


@router.delete(
    "/backup/token",
    status_code=204,
    response_class=Response,
    responses={
        401: {"description": "No valid session"},
        409: {"description": "secrets.yaml cannot be written"},
    },
)
def delete_backup_token(runtime: Runtime, peer: ClientAddress, session: CurrentSession) -> Response:
    """Remove the backup token. A script that holds it gets 401 from then on."""
    store = runtime.store
    with store.lock() as held:
        tree = load_tree(store.secrets_path)
        tree.pop("backup_token_hash", None)
        tree.pop("backup_token_created_at", None)
        store.save_secrets(tree, held)
    store.audit.record("admin", "backup.token_delete", {"address": peer})
    return Response(status_code=204)


@router.post(
    "/backup/check",
    response_model=BackupCheck,
    openapi_extra=ARCHIVE_BODY,
    responses={401: {"description": "No valid session"}, **UPLOAD_RESPONSES},
)
async def check_uploaded_backup(
    request: Request, runtime: Runtime, session: CurrentSession
) -> BackupCheck:
    """Describe a backup and change nothing. The body is the raw `application/gzip` archive."""
    return BackupCheck.of(await with_upload(request, runtime.store.root, check_backup))


@router.post(
    "/backup/restore",
    status_code=204,
    response_class=Response,
    openapi_extra=ARCHIVE_BODY,
    responses={401: {"description": "No valid session"}, **UPLOAD_RESPONSES},
)
async def restore_uploaded_backup(
    request: Request,
    runtime: Runtime,
    peer: ClientAddress,
    session: CurrentSession,
    services: ServicesDep,
) -> Response:
    """Restore a backup over this install. The body is the raw `application/gzip` archive.

    Everything is the backup's, the password too, and every session ends, this one included. The
    dashboard keeps its listen address and restarts.
    """
    listen = runtime.store.load_config().dashboard.listen
    await with_upload(
        request, runtime.store.root, lambda staging: restore_backup(runtime.store, staging, listen)
    )
    return await finish_restore(runtime, services, peer)
