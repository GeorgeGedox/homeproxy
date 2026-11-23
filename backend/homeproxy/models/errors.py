"""Validation errors that are safe to log or return.

A `pydantic.ValidationError` is not safe. Its `str()`, `errors()` and `json()` can show input values
and the keys of the input. A secret pasted into a key position, such as a hash used as a basic auth
user name, appears in the error location even with `include_input=False`. A key or a route `type`
can also hold a raw newline, which forges a log line.
"""

from typing import TypedDict

from pydantic import ValidationError
from pydantic_core import ErrorDetails

from homeproxy.models.base import StrictModel
from homeproxy.models.fields import PATTERN_MESSAGES

KEY_PLACEHOLDER = "<key>"

# Route types appear first in the location of a route error. "[key]" is pydantic's own marker for
# an error in a dict key.
_FIXED_SEGMENTS = {"http", "passthrough", "tcp", "redirect", "[key]"}


class SafeError(TypedDict):
    """One validation error, holding only text from the schema."""

    loc: tuple[str | int, ...]
    type: str
    msg: str


def _field_names() -> set[str]:
    """Return the field names of every `StrictModel`, the only names known not to be user data."""
    names = set(_FIXED_SEGMENTS)
    pending: list[type[StrictModel]] = [StrictModel]
    while pending:
        model = pending.pop()
        pending.extend(model.__subclasses__())
        names.update(model.model_fields)
    return names


def _plain_message(item: ErrorDetails) -> str:
    """Return pydantic's message in plain words, without a regex or a `Value error, ` prefix."""
    context = item.get("ctx", {})
    match item["type"]:
        case "string_pattern_mismatch":
            return PATTERN_MESSAGES.get(
                str(context.get("pattern")), "has a character that is not allowed"
            )
        case "value_error":
            return item["msg"].removeprefix("Value error, ")
        case "missing":
            return "is required"
        case "int_parsing":
            return "must be a whole number"
        case "greater_than_equal":
            return f"must be at least {context.get('ge')}"
        case "less_than_equal":
            return f"must be at most {context.get('le')}"
        case "too_short" if context.get("min_length") == 1:
            return "needs at least one value"
    return item["msg"]


def safe_errors(error: ValidationError) -> list[SafeError]:
    """Return the errors with no input values and no text from the input's keys.

    A location segment stays only if it is a list index or a field name of a `StrictModel`.
    Anything else is a dict key or an unknown key, and becomes `KEY_PLACEHOLDER`. So an error in a
    header value has the location `("http", "response_headers", "<key>")`, and a field of a model
    that does not derive from `StrictModel` also becomes the placeholder.

    A missing or unknown route `type` gets the location `("type",)`, without the value sent.
    """
    known = _field_names()
    details = error.errors(include_input=False, include_url=False)
    # An integer key looks like a list index unless one of its errors marks it as a key.
    key_paths = {
        item["loc"][: index + 1]
        for item in details
        for index in range(len(item["loc"]) - 1)
        if item["loc"][index + 1] == "[key]"
    } | {item["loc"] for item in details if item["type"] == "invalid_key"}

    result: list[SafeError] = []
    for item in details:
        loc: list[str | int] = []
        for index, segment in enumerate(item["loc"]):
            is_key = item["loc"][: index + 1] in key_paths
            safe = not is_key and (isinstance(segment, int) or segment in known)
            loc.append(segment if safe else KEY_PLACEHOLDER)

        message = _plain_message(item)
        if item["type"] in {"union_tag_invalid", "union_tag_not_found"}:
            context = item.get("ctx", {})
            loc.append(str(context.get("discriminator", "")).strip("'"))
            if item["type"] == "union_tag_invalid":
                # Pydantic's message repeats the tag from the input.
                message = f"Input should be one of: {context.get('expected_tags', '')}"
        result.append({"loc": tuple(loc), "type": item["type"], "msg": message})
    return result
