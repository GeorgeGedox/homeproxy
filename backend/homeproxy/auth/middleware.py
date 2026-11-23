"""Session gate and Origin check for every request.

Every path under `/api/` needs a valid session, except the few in `PUBLIC_API_PATHS`. So an
endpoint added by a later task is protected without doing anything. Every request that can change
state, which is any method other than GET, HEAD and OPTIONS, must carry an `Origin` header equal to
the dashboard's own origin. Browsers always send it on such requests, so a page on another site
cannot submit a form or a fetch to the dashboard (CSRF). The `SameSite=Strict` cookie is a second
defense.
"""

from collections.abc import Callable

from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers
from starlette.requests import cookie_parser
from starlette.types import ASGIApp, Receive, Scope, Send

from homeproxy.auth import errors
from homeproxy.auth.listen import WINDOW_SCOPE_KEY
from homeproxy.auth.runtime import AuthRuntime
from homeproxy.auth.sessions import COOKIE_NAME, Session

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

PUBLIC_API_PATHS = frozenset(
    {
        "/api/v1/healthz",
        "/api/v1/setup/status",
        "/api/v1/setup",
        "/api/v1/auth/login",
    }
)
"""API paths that need no session. Setup checks its own token, and login its own password."""

SESSION_STATE_KEY = "auth_session"
"""Key in `request.state` that holds the `Session` of an authenticated request."""


def is_api_path(path: str) -> bool:
    """Return whether a path is under `/api`. Extra leading slashes do not hide it."""
    return path.lstrip("/").split("/", 1)[0] == "api"


def origin_allowed(scope: Scope, headers: Headers) -> bool:
    """Return whether the `Origin` header names the origin the request was sent to."""
    origin = headers.get("origin")
    host = headers.get("host")
    if not origin or not host:
        return False
    expected = f"{scope.get('scheme', 'https')}://{host}"
    return origin.lower() == expected.lower()


def session_cookie(headers: Headers) -> str | None:
    """Return the session cookie value from the request headers, if any."""
    cookies = cookie_parser(headers.get("cookie", ""))
    return cookies.get(COOKIE_NAME)


class AuthMiddleware:
    """Pure ASGI middleware: Origin check on changes, then the session gate on `/api/`."""

    def __init__(self, app: ASGIApp, runtime: Callable[[], AuthRuntime]) -> None:
        """Wrap `app`. `runtime` returns the process's `AuthRuntime`."""
        self.app = app
        self.runtime = runtime

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Check the request, then pass it on or answer with 401 or 403."""
        # The server runs with WebSockets off (`ws="none"`), so only http and lifespan arrive.
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        if scope["method"] not in SAFE_METHODS and not origin_allowed(scope, headers):
            error = errors.AuthError(
                403, "origin_mismatch", "The request did not come from the dashboard's own page."
            )
            await errors.error_response(error)(scope, receive, send)
            return
        path = scope["path"]
        if is_api_path(path) and path not in PUBLIC_API_PATHS:
            cookie = session_cookie(headers)
            session: Session | None = None
            if cookie:
                runtime = await run_in_threadpool(self.runtime)
                session = await run_in_threadpool(runtime.authenticate, cookie)
            if session is None:
                await errors.error_response(errors.unauthorized())(scope, receive, send)
                return
            scope.setdefault("state", {})[SESSION_STATE_KEY] = session
            if WINDOW_SCOPE_KEY in scope:
                # A session reached a new listen address on trial, which confirms it.
                await runtime.listen_change.confirm(scope[WINDOW_SCOPE_KEY])
        await self.app(scope, receive, send)
