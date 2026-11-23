"""Auth failures, in the API's error shape: `{"error": {"code", "message", "field"}}`."""

from fastapi import Request
from fastapi.responses import JSONResponse


class AuthError(Exception):
    """A request the auth layer refuses. The message is safe to show and holds no secret."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        field: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        """Describe the refusal: HTTP status, a stable code and a message for the user."""
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.field = field
        self.headers = headers or {}


def error_body(code: str, message: str, field: str | None = None) -> dict[str, object]:
    """Return the API's error body."""
    return {"error": {"code": code, "message": message, "field": field}}


def error_response(error: AuthError) -> JSONResponse:
    """Turn an `AuthError` into a JSON response."""
    return JSONResponse(
        error_body(error.code, error.message, error.field),
        status_code=error.status,
        headers=error.headers,
    )


async def auth_error_handler(request: Request, error: Exception) -> JSONResponse:
    """FastAPI exception handler for `AuthError`."""
    if not isinstance(error, AuthError):  # pragma: no cover - registered for AuthError only
        raise error
    return error_response(error)


def unauthorized() -> AuthError:
    """No valid session."""
    return AuthError(401, "unauthorized", "Log in to continue.")


def throttled(retry_after: int) -> AuthError:
    """Too many failed attempts from this client."""
    return AuthError(
        429,
        "too_many_attempts",
        f"Too many failed attempts. Try again in {retry_after} seconds.",
        headers={"Retry-After": str(retry_after)},
    )


def setup_required() -> AuthError:
    """No password exists yet."""
    return AuthError(409, "setup_required", "HomeProxy is not set up yet. Finish setup first.")


def changes_pending() -> AuthError:
    """A change saved at once is refused while staged changes wait for an apply."""
    return AuthError(409, "changes_pending", "Apply or discard pending changes first.")


def unavailable(message: str) -> AuthError:
    """The files auth needs cannot be read."""
    return AuthError(503, "auth_unavailable", message)
