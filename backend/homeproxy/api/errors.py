"""Validation failures in the API's error shape, with no input values and no input keys.

FastAPI's default handler for a bad request body returns the input, and the input can be a
password or a token. Its error locations can also hold a secret pasted into a key position. So
both handlers here pass every error through `safe_errors` first.
"""

import logging
from collections.abc import Sequence
from typing import get_args

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from pydantic_core import InitErrorDetails, PydanticCustomError
from pydantic_core.core_schema import ErrorType

from homeproxy.auth.errors import error_body
from homeproxy.models import SafeError, safe_errors
from homeproxy.store.errors import StoreError, ValidationFailed
from homeproxy.store.lock import LockTimeout

logger = logging.getLogger(__name__)


_ROUTE_TYPES = frozenset({"http", "passthrough", "tcp", "redirect"})
_PYDANTIC_ERROR_TYPES = frozenset(get_args(ErrorType))


def _field(loc: tuple[str | int, ...]) -> str:
    """Return the API `field` of an error location. A route error starts with the route type,
    as in `("http", "hosts", 0)`, which is not a field of the request."""
    if loc and loc[0] in _ROUTE_TYPES:
        loc = loc[1:]
    return ".".join(str(segment) for segment in loc)


def validation_response(errors: Sequence[SafeError]) -> JSONResponse:
    """Return 422 naming the first failing field and listing every error."""
    fields = [_field(error["loc"]) for error in errors]
    message = "; ".join(
        f"{field}: {error['msg']}" if field else error["msg"]
        for field, error in zip(fields, errors, strict=True)
    )
    first = fields[0] if fields else None
    return JSONResponse(
        error_body("validation_failed", message or "The request is not valid.", first or None),
        status_code=422,
    )


def _as_validation_error(error: RequestValidationError) -> ValidationError:
    """Rebuild pydantic's error from FastAPI's copy, dropping the input and the `body` prefix."""
    details: list[InitErrorDetails] = []
    for item in error.errors():
        # A JSON syntax error's location is a character offset, not a field.
        loc = () if item["type"] == "json_invalid" else tuple(item["loc"][1:])
        kind: str | PydanticCustomError = item["type"]
        if kind not in _PYDANTIC_ERROR_TYPES:
            # A model's own error, such as `redirect_loop`, is rebuilt from its message. The models
            # write those messages without the input.
            kind = PydanticCustomError(item["type"], item["msg"])
        detail: InitErrorDetails = {"type": kind, "loc": loc, "input": None}
        if "ctx" in item:
            detail["ctx"] = item["ctx"]
        details.append(detail)
    return ValidationError.from_exception_data("request", details)


async def request_validation_handler(request: Request, error: Exception) -> JSONResponse:
    """FastAPI handler for a request that does not match its model."""
    if not isinstance(error, RequestValidationError):  # pragma: no cover - registered for it only
        raise error
    try:
        errors = safe_errors(_as_validation_error(error))
    except (KeyError, TypeError, ValueError) as failure:
        # An error type pydantic cannot rebuild. Say less rather than risk echoing the input, and
        # log only the error type names, which come from the schema.
        types = sorted({str(item.get("type")) for item in error.errors()})
        logger.error(
            "cannot rebuild request validation errors (%s): %s", type(failure).__name__, types
        )
        errors = []
    return validation_response(errors)


async def store_error_handler(request: Request, error: Exception) -> JSONResponse:
    """FastAPI handler for store errors. Their text never holds file content."""
    if isinstance(error, ValidationFailed):
        return validation_response(error.errors)
    if isinstance(error, LockTimeout):
        return JSONResponse(error_body("busy", str(error)), status_code=409)
    return JSONResponse(error_body("config_error", str(error)), status_code=409)


def install_error_handlers(app: FastAPI) -> None:
    """Replace FastAPI's validation handler and answer store errors in the API's error shape."""
    app.add_exception_handler(RequestValidationError, request_validation_handler)
    app.add_exception_handler(StoreError, store_error_handler)
