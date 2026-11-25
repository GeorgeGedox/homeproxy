"""Session endpoints: setup status, login, logout and the current session.

`POST /setup` and `PUT /auth/password` belong to the settings API. They use
`AuthRuntime.setup_claim` and `hash_password` from this package.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field, ValidationError
from starlette.concurrency import run_in_threadpool

from homeproxy.auth import errors
from homeproxy.auth.middleware import SESSION_STATE_KEY
from homeproxy.auth.passwords import MAX_LENGTH
from homeproxy.auth.runtime import AuthRuntime, get_runtime
from homeproxy.auth.sessions import COOKIE_NAME, Session
from homeproxy.auth.setup_detection import DetectedSetup, detect_host_setup, taken_ports
from homeproxy.models import StrictModel

router = APIRouter(prefix="/api/v1", tags=["auth"])

_MAX_BODY = 8192


def auth_runtime(request: Request) -> AuthRuntime:
    """FastAPI dependency: the process's `AuthRuntime`."""
    return get_runtime(request.app)


def client_address(request: Request) -> str:
    """FastAPI dependency: the client's IP address as the socket reports it.

    The server never trusts `X-Forwarded-For`, so a client cannot choose its address.
    """
    return request.client.host if request.client else ""


def current_session(request: Request) -> Session:
    """FastAPI dependency: the session the middleware checked. Raises 401 without one."""
    session = getattr(request.state, SESSION_STATE_KEY, None)
    if not isinstance(session, Session):
        raise errors.unauthorized()
    return session


Runtime = Annotated[AuthRuntime, Depends(auth_runtime)]
ClientAddress = Annotated[str, Depends(client_address)]
CurrentSession = Annotated[Session, Depends(current_session)]


class SetupStatus(BaseModel):
    """Whether the setup wizard must run. While it must: the network values setup will write,
    and the setup request fields the request must carry: the values it cannot detect, and the
    HTTP or HTTPS port when another program listens on the default one."""

    setup_required: bool
    detected: DetectedSetup | None = None
    needs: list[str] | None = None


class LoginRequest(StrictModel):
    """The dashboard password."""

    password: str = Field(min_length=1, max_length=MAX_LENGTH)


class Me(BaseModel):
    """The current session."""

    authenticated: bool


def _error(description: str) -> dict[str, object]:
    return {"description": description}


@router.get("/setup/status", response_model=SetupStatus, response_model_exclude_none=True)
def setup_status(runtime: Runtime) -> SetupStatus:
    """Report whether setup is still needed, with the detected network values while it is.

    Needs no session. The values are this host's own LAN addresses.
    """
    if not runtime.setup_required():
        return SetupStatus(setup_required=False)
    detected = detect_host_setup()
    needs = detected.missing() + taken_ports()
    return SetupStatus(setup_required=True, detected=detected, needs=needs)


async def _read_password(request: Request) -> str:
    """Read `{"password": "..."}` without echoing the body in any error."""
    body = await request.body()
    invalid = errors.AuthError(
        422, "invalid_request", "Send a JSON object with a password string.", field="password"
    )
    if len(body) > _MAX_BODY:
        raise invalid
    try:
        return LoginRequest.model_validate_json(body).password
    except ValidationError:
        raise invalid from None


def set_session_cookie(response: Response, value: str, ttl: int) -> None:
    """Set the session cookie: HttpOnly, Secure, SameSite=Strict, path `/`."""
    response.set_cookie(
        COOKIE_NAME,
        value,
        max_age=ttl,
        path="/",
        secure=True,
        httponly=True,
        samesite="strict",
    )


def clear_session_cookie(response: Response) -> None:
    """Tell the browser to drop the session cookie."""
    response.delete_cookie(COOKIE_NAME, path="/", secure=True, httponly=True, samesite="strict")


@router.post(
    "/auth/login",
    status_code=204,
    response_class=Response,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": LoginRequest.model_json_schema()}},
        }
    },
    responses={
        401: _error("Wrong password"),
        409: _error("Setup has not run yet"),
        422: _error("Malformed request"),
        429: _error("Too many failed attempts; see Retry-After"),
        503: _error("config.yaml or secrets.yaml cannot be read"),
    },
)
async def login(request: Request, runtime: Runtime, peer: ClientAddress) -> Response:
    """Check the password and start a session. Five failures in 15 minutes lock the client out
    for the rest of that window. Needs no session."""
    password = await _read_password(request)
    cookie, ttl = await run_in_threadpool(runtime.login, peer, password)
    response = Response(status_code=204)
    set_session_cookie(response, cookie, ttl)
    return response


@router.post(
    "/auth/logout",
    status_code=204,
    response_class=Response,
    responses={401: _error("No valid session")},
)
def logout(runtime: Runtime, session: CurrentSession) -> Response:
    """End the current session and clear its cookie."""
    runtime.logout(session)
    response = Response(status_code=204)
    clear_session_cookie(response)
    return response


@router.get("/auth/me", response_model=Me, responses={401: _error("No valid session")})
def me(session: CurrentSession) -> Me:
    """Report that the session is valid. Answers 401 without one."""
    return Me(authenticated=True)
