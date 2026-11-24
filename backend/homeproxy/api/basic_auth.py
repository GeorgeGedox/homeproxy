"""Basic auth users of a route. They live in `secrets.yaml`, which has no pending copy, so a change
is written at once and takes effect at the next apply."""

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, SecretStr

from homeproxy.api.changes import edit_lock
from homeproxy.api.errors import ApiError
from homeproxy.api.routes import read_route
from homeproxy.auth.routes import Runtime
from homeproxy.models import HttpRoute, StrictModel
from homeproxy.models.fields import BasicAuthUser
from homeproxy.store.basic_auth import BasicAuthError, set_basic_auth

router = APIRouter(prefix="/api/v1", tags=["routes"])

_NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"description": "No route has this name"}}


class BasicAuthChange(StrictModel):
    """The route's users after the change. A password sets that user's password, null keeps the
    current one, and a user left out is removed. Passwords need 8 to 1024 characters."""

    users: dict[BasicAuthUser, SecretStr | None]


class BasicAuthUsers(BaseModel):
    """The names of a route's basic auth users. Passwords and hashes are never returned."""

    users: list[str]


@router.get("/routes/{name}/basic-auth", response_model=BasicAuthUsers, responses=_NOT_FOUND)
def get_basic_auth(name: str, runtime: Runtime) -> BasicAuthUsers:
    """Return the names of a route's basic auth users."""
    with edit_lock(runtime.store) as held:
        read_route(runtime.store, name, held)
    return BasicAuthUsers(users=sorted(runtime.store.load_secrets().basic_auth.get(name, {})))


@router.put(
    "/routes/{name}/basic-auth",
    response_model=BasicAuthUsers,
    responses=_NOT_FOUND | {400: {"description": "A password or the user set is refused"}},
)
def put_basic_auth(name: str, body: BasicAuthChange, runtime: Runtime) -> BasicAuthUsers:
    """Set a route's basic auth users in `secrets.yaml`. This is not a pending change: it takes
    effect at the next apply, which works even with nothing pending."""
    store = runtime.store
    with edit_lock(store) as held:
        route = read_route(store, name, held)[1]
        if not body.users and isinstance(route, HttpRoute) and route.auth.type == "basic":
            raise ApiError(
                400, "users_required", "A route with basic auth needs a user.", field="users"
            )
        passwords = {
            user: None if password is None else password.get_secret_value()
            for user, password in body.users.items()
        }
        try:
            set_basic_auth(store, name, passwords, held)
        except BasicAuthError as error:
            raise ApiError(400, "invalid_basic_auth", str(error), field="users") from None
        users = sorted(store.load_secrets().basic_auth.get(name, {}))
    return BasicAuthUsers(users=users)
