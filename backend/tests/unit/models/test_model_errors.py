"""`safe_errors` returns no input values and no text from the input's keys."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError
from ruamel.yaml import YAML

from homeproxy.logs.stream import LogFilter
from homeproxy.models import Config, SafeError, Secrets, parse_route, safe_errors

SESSION_KEY = "A" * 43 + "="
SECRET_HASH = f"$6$saltsalt${'a' * 86}"
SECRET_TOKEN = "Secret-Token-Value-123"  # noqa: S105
INVALID = Path(__file__).parent / "invalid"


def errors_of(validate: Callable[[Any], object], data: Any) -> list[SafeError]:
    with pytest.raises(ValidationError) as info:
        validate(data)
    return safe_errors(info.value)


def secrets(**changes: Any) -> dict[str, Any]:
    return {"version": 1, "session_key": SESSION_KEY, **changes}


def http_route(**changes: Any) -> dict[str, Any]:
    return {
        "version": 1,
        "name": "app",
        "type": "http",
        "hosts": ["app.example.com"],
        "exposure": "lan",
        "upstreams": [{"address": "192.168.1.20:8080"}],
        **changes,
    }


class Wrapper(BaseModel):
    """A caller's own model that is not a `StrictModel` and has no strict settings."""

    inner: Secrets


INTEGER_KEY: dict[Any, Any] = {**secrets(), 123456: "x"}

SECRET_KEY_CASES = [
    pytest.param(
        secrets(basic_auth={"r": {SECRET_HASH: SECRET_HASH}}),
        SECRET_HASH,
        [("basic_auth", "<key>", "<key>", "[key]")],
        id="hash-as-user",
    ),
    pytest.param(
        secrets(basic_auth={"r": {SECRET_TOKEN: "bad"}}),
        SECRET_TOKEN,
        [("basic_auth", "<key>", "<key>")],
        id="valid-looking-user",
    ),
    pytest.param(
        secrets(dns_credentials={SECRET_TOKEN: {"provider": "cloudflare", "api_token": "ok"}}),
        SECRET_TOKEN,
        [("dns_credentials", "<key>", "[key]")],
        id="token-as-credential-name",
    ),
    pytest.param(
        secrets(**{SECRET_TOKEN: "x"}),
        SECRET_TOKEN,
        [("<key>",)],
        id="token-as-unknown-key",
    ),
    pytest.param(
        secrets(crowdsec={SECRET_TOKEN: "x"}),
        SECRET_TOKEN,
        [("crowdsec", "<key>")],
        id="token-as-nested-unknown-key",
    ),
    pytest.param(
        secrets(basic_auth={"r": {123456: "bad"}}),
        "123456",
        [("basic_auth", "<key>", "<key>", "[key]"), ("basic_auth", "<key>", "<key>")],
        id="integer-as-user",
    ),
    pytest.param(
        INTEGER_KEY,
        "123456",
        [("<key>",)],
        id="integer-as-unknown-key",
    ),
]


@pytest.mark.parametrize(("data", "secret", "locations"), SECRET_KEY_CASES)
def test_secret_in_key_position_does_not_appear(
    data: dict[Any, Any], secret: str, locations: list[tuple[str | int, ...]]
) -> None:
    errors = errors_of(Secrets.model_validate, data)
    assert [error["loc"] for error in errors] == locations
    assert secret not in str(errors)


@pytest.mark.parametrize(("data", "secret", "locations"), SECRET_KEY_CASES)
def test_secret_stays_hidden_under_an_outer_validator(
    data: dict[Any, Any], secret: str, locations: list[tuple[str | int, ...]]
) -> None:
    adapter: TypeAdapter[list[Secrets]] = TypeAdapter(list[Secrets])
    in_list = errors_of(adapter.validate_python, [data])
    assert [error["loc"] for error in in_list] == [(0, *loc) for loc in locations]
    assert secret not in str(in_list)

    in_model = errors_of(Wrapper.model_validate, {"inner": data})
    assert [error["loc"] for error in in_model] == [("<key>", *loc) for loc in locations]
    assert secret not in str(in_model)


@pytest.mark.parametrize(
    "data",
    [
        secrets(session_key=f"{SECRET_TOKEN}\n"),
        secrets(
            dns_credentials={"cf": {"provider": "cloudflare", "api_token": f"{SECRET_TOKEN}\n"}}
        ),
        secrets(crowdsec={"bouncer_key": f"{SECRET_TOKEN}\n"}),
    ],
    ids=["session_key", "api_token", "bouncer_key"],
)
def test_secret_in_value_position_does_not_appear(data: dict[str, Any]) -> None:
    with pytest.raises(ValidationError) as info:
        Secrets.model_validate(data)
    # `hide_input_in_errors` keeps it out of the raw text too, while the model is outermost.
    assert SECRET_TOKEN not in str(info.value)
    assert SECRET_TOKEN not in str(safe_errors(info.value))
    adapter: TypeAdapter[list[Secrets]] = TypeAdapter(list[Secrets])
    assert SECRET_TOKEN not in str(errors_of(adapter.validate_python, [data]))


def test_errors_hold_only_location_type_and_message() -> None:
    errors = errors_of(Secrets.model_validate, secrets(session_key="bad"))
    assert errors == [
        {
            "loc": ("session_key",),
            "type": "value_error",
            "msg": "must be base64",
        }
    ]


@pytest.mark.parametrize(
    ("changes", "loc", "message"),
    [
        ({"hosts": ["App.example.com"]}, ("http", "hosts", 0), "must be a DNS name in lowercase"),
        ({"hosts": []}, ("http", "hosts"), "needs at least one value"),
        ({"exposure": None}, ("http", "exposure"), "Input should be"),
        ({"upstreams": [{}]}, ("http", "upstreams", 0, "address"), "is required"),
        ({"request_headers": {"X-A": "a\tb"}}, ("http", "request_headers", "<key>"), "printable"),
    ],
    ids=["pattern", "empty-list", "enum", "missing", "pattern-in-a-key-position"],
)
def test_messages_are_plain_words(
    changes: dict[str, Any], loc: tuple[Any, ...], message: str
) -> None:
    errors = errors_of(parse_route, http_route(**changes))
    assert [error["loc"] for error in errors] == [loc]
    assert message in errors[0]["msg"]
    assert "pattern" not in errors[0]["msg"]


@pytest.mark.parametrize(
    ("status", "message"),
    [
        ("abc", "must be a whole number"),
        ("99", "must be at least 100"),
        ("600", "must be at most 599"),
    ],
    ids=["not-a-number", "too-low", "too-high"],
)
def test_a_bad_log_filter_names_its_field_in_plain_words(status: str, message: str) -> None:
    errors = errors_of(LogFilter.model_validate, {"status": status})
    assert [(error["loc"], error["msg"]) for error in errors] == [(("status",), message)]


def test_a_range_with_host_bits_says_so() -> None:
    errors = errors_of(Config.model_validate, YAML(typ="rt").load(INVALID / "config-bad-cidr.yaml"))
    assert errors[0]["msg"].startswith("has host bits set")


@pytest.mark.parametrize(
    "value",
    ["evil\nforged log line", 123456, ["evil\nline"], {"evil\nkey": 1}],
    ids=["newline", "integer", "list", "dict"],
)
def test_unknown_route_type_is_not_echoed(value: object) -> None:
    errors = errors_of(parse_route, {"name": "app", "type": value})
    assert errors == [
        {
            "loc": ("type",),
            "type": "union_tag_invalid",
            "msg": "Input should be one of: 'http', 'passthrough', 'tcp', 'redirect'",
        }
    ]


def test_missing_route_type_names_the_field() -> None:
    errors = errors_of(parse_route, {"name": "app"})
    assert [error["loc"] for error in errors] == [("type",)]
    assert errors[0]["type"] == "union_tag_not_found"


def test_field_names_and_list_indexes_stay_readable() -> None:
    errors = errors_of(parse_route, http_route(hosts=["bad\nhost"], auth={"type": "x"}))
    assert [error["loc"] for error in errors] == [
        ("http", "hosts", 0),
        ("http", "auth", "type"),
    ]


def test_field_of_another_route_type_stays_readable() -> None:
    data = http_route(type="passthrough", auth={"type": "none"})
    assert [error["loc"] for error in errors_of(parse_route, data)] == [("passthrough", "auth")]


def test_header_key_and_value_stay_out_of_a_value_error() -> None:
    with pytest.raises(ValidationError) as info:
        parse_route(http_route(request_headers={"X-Api-Key": f"{SECRET_TOKEN}\n"}))
    assert SECRET_TOKEN not in str(info.value)
    errors = safe_errors(info.value)
    assert [error["loc"] for error in errors] == [("http", "request_headers", "<key>")]
    assert "X-Api-Key" not in str(errors)
    assert SECRET_TOKEN not in str(errors)


def test_bad_header_name_keeps_the_key_marker() -> None:
    errors = errors_of(parse_route, http_route(request_headers={"X-Evil\nforged": "ok"}))
    assert [error["loc"] for error in errors] == [("http", "request_headers", "<key>", "[key]")]


def validate_for(path: Path) -> Callable[[Any], object]:
    if path.stem.startswith("route-"):
        return parse_route
    return Config.model_validate if path.stem.startswith("config-") else Secrets.model_validate


@pytest.mark.parametrize("path", sorted(INVALID.glob("*.yaml")), ids=lambda path: path.stem)
def test_no_invalid_example_puts_a_control_character_in_an_error(path: Path) -> None:
    errors = errors_of(validate_for(path), YAML(typ="rt").load(path))
    assert errors
    assert all(character.isprintable() for character in str(errors))
    for error in errors:
        assert all(isinstance(segment, int) or segment.isprintable() for segment in error["loc"])
        assert error["msg"].isprintable()
        assert set(error) == {"loc", "type", "msg"}
