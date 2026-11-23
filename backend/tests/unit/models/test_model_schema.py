"""The JSON schema export."""

import json
import re
from collections.abc import Iterator
from typing import Any

import pytest

from homeproxy.models.schema import json_schema, main


def walk(node: Any) -> Iterator[dict[str, Any]]:
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from walk(value)


def test_root_holds_every_file_model() -> None:
    assert set(json_schema()["properties"]) == {"config", "route", "domain", "secrets"}


def test_every_reference_resolves_in_one_document() -> None:
    schema = json_schema()
    references = [node["$ref"] for node in walk(schema) if "$ref" in node]
    assert references
    for reference in references:
        assert reference.startswith("#/$defs/")
        assert reference.removeprefix("#/$defs/") in schema["$defs"]


def test_route_is_discriminated_by_type() -> None:
    route = json_schema()["properties"]["route"]
    assert route["discriminator"]["propertyName"] == "type"
    assert set(route["discriminator"]["mapping"]) == {"http", "passthrough", "tcp", "redirect"}


def test_route_types_show_only_their_own_fields() -> None:
    defs = json_schema()["$defs"]
    assert "auth" in defs["HttpRoute"]["properties"]
    assert "auth" not in defs["PassthroughRoute"]["properties"]
    assert "hosts" not in defs["TcpRoute"]["properties"]
    assert "listen_port" in defs["TcpRoute"]["required"]


def test_strict_patterns_reach_the_ui() -> None:
    defs = json_schema()["$defs"]
    assert defs["HttpRoute"]["properties"]["name"]["pattern"] == "^[a-z0-9][a-z0-9-]{0,62}$"
    assert "pattern" in defs["HttpRoute"]["properties"]["hosts"]["items"]
    assert "pattern" in defs["Upstream"]["properties"]["address"]


def test_patterns_are_portable_to_the_browser() -> None:
    patterns = [
        node["pattern"] for node in walk(json_schema()) if isinstance(node.get("pattern"), str)
    ]
    assert patterns
    for pattern in patterns:
        assert re.compile(pattern)
        assert "(?" not in pattern
        assert "\\d" not in pattern
        assert pattern.startswith("^")
        assert pattern.endswith("$")


def test_secret_fields_are_write_only() -> None:
    defs = json_schema()["$defs"]
    assert defs["Dashboard"]["properties"]["password_hash"]["writeOnly"] is True
    credential = defs["DnsCredential"]["properties"]
    assert credential["api_token"]["anyOf"][0]["writeOnly"] is True
    assert all(v["writeOnly"] for v in credential["env"]["patternProperties"].values())
    assert defs["Secrets"]["properties"]["session_key"]["writeOnly"] is True


def test_schema_holds_no_example_values() -> None:
    text = json.dumps(json_schema())
    for needle in ("fake-", "homeproxy-ui", "$argon2id$v=19$m=65536", "192.168.", "example.com"):
        assert needle not in text


def test_main_prints_the_schema_as_json(capsys: pytest.CaptureFixture[str]) -> None:
    main()
    assert json.loads(capsys.readouterr().out) == json_schema()
