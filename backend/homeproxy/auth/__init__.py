"""Dashboard auth: setup mode, password, sessions, login rate limit, Origin check and TLS.

`install_auth(app)` adds the session gate, the Origin check and the session endpoints to the
FastAPI app. `homeproxy serve` (see `homeproxy.auth.server`) runs the app over HTTPS and drops
connections from addresses outside the allowed ranges before any HTTP is read.
"""

from fastapi import FastAPI

from homeproxy.auth.errors import AuthError, auth_error_handler
from homeproxy.auth.middleware import AuthMiddleware
from homeproxy.auth.routes import router
from homeproxy.auth.runtime import AuthRuntime, get_runtime


def install_auth(app: FastAPI) -> None:
    """Protect every `/api/` path with the session gate and add the session endpoints."""
    app.add_middleware(AuthMiddleware, runtime=lambda: get_runtime(app))
    app.add_exception_handler(AuthError, auth_error_handler)
    app.include_router(router)


__all__ = ["AuthError", "AuthRuntime", "get_runtime", "install_auth"]
