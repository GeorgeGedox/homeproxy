"""`PUT /auth/password`: change the dashboard password from the dashboard.

Login, logout and the session check live in `homeproxy.auth.routes`.
"""

import logging

from fastapi import APIRouter, Response
from pydantic import SecretStr
from ruamel.yaml.scalarstring import DoubleQuotedScalarString

from homeproxy.api.errors import ApiError, changes_pending
from homeproxy.auth import errors
from homeproxy.auth.passwords import PasswordRejected, hash_password, verify_password
from homeproxy.auth.routes import ClientAddress, CurrentSession, Runtime, set_session_cookie
from homeproxy.models import StrictModel
from homeproxy.store.pending import PendingChanges
from homeproxy.store.tree import load_tree

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["auth"])


class PasswordChange(StrictModel):
    """The current password, to confirm, and the new one."""

    current_password: SecretStr
    new_password: SecretStr


def new_password_hash(password: SecretStr, field: str) -> str:
    """Hash a new dashboard password, or raise a 422 `ApiError` that names `field`."""
    try:
        return hash_password(password.get_secret_value())
    except PasswordRejected as error:
        raise ApiError(422, "invalid_password", str(error), field=field) from None


@router.put(
    "/auth/password",
    status_code=204,
    response_class=Response,
    responses={
        401: {"description": "No valid session"},
        403: {"description": "The current password is wrong"},
        409: {"description": "config.yaml is staged; apply or discard it first"},
        422: {"description": "The new password does not meet the rules"},
        429: {"description": "Too many failed attempts; see Retry-After"},
    },
)
def change_password(
    body: PasswordChange, runtime: Runtime, peer: ClientAddress, session: CurrentSession
) -> Response:
    """Set a new password and sign out every other session. This one gets a new cookie.

    Refused while `config.yaml` is staged: the staged copy holds the old hash, so applying it would
    bring the old password back.
    """
    wait = runtime.throttle.retry_after(peer)
    if wait is not None:
        raise errors.throttled(wait)
    current = runtime.settings()
    if not verify_password(current.password_hash, body.current_password.get_secret_value()):
        runtime.throttle.record_failure(peer)
        logger.warning("wrong current password in a password change from %s", peer)
        raise ApiError(
            403, "invalid_password", "The current password is wrong.", field="current_password"
        )
    store = runtime.store
    with store.lock() as held:
        if PendingChanges(store).config_staged(held):
            raise changes_pending()
        password_hash = new_password_hash(body.new_password, "new_password")
        tree = load_tree(store.config_path)
        tree["dashboard"]["password_hash"] = DoubleQuotedScalarString(password_hash)
        store.save_config(tree, held)
    runtime.throttle.reset(peer)
    logger.info("dashboard password changed from %s", peer)
    response = Response(status_code=204)
    set_session_cookie(response, runtime.signer().issue(password_hash), current.session_ttl)
    return response
