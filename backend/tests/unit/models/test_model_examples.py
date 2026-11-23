"""The seeded examples load, and the invalid examples fail on the expected field."""

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from ruamel.yaml import YAML

from homeproxy.models import Config, HttpRoute, Secrets, parse_route

DATA = Path(__file__).parents[4] / "examples" / "data"
ROUTES = DATA / "config" / "routes"
INVALID = Path(__file__).parent / "invalid"
ROUTE_FILES = sorted(ROUTES.glob("*.yaml"))


def load_yaml(path: Path) -> Any:
    """Load with the round-trip loader the store uses, because its types differ from plain YAML."""
    return YAML(typ="rt").load(path)


def test_config_example_loads() -> None:
    config = Config.model_validate(load_yaml(DATA / "config" / "config.yaml"))
    assert [domain.zone for domain in config.domains] == ["example.com", "example.net"]


def test_secrets_example_loads() -> None:
    secrets = Secrets.model_validate(load_yaml(DATA / "secrets" / "secrets.yaml"))
    # The Cloudflare form from before any lego provider was offered loads unchanged.
    assert set(secrets.dns_credentials) == {"cf-main", "cf-second"}
    assert all(c.provider == "cloudflare" and c.api_token for c in secrets.dns_credentials.values())


@pytest.mark.parametrize("path", ROUTE_FILES, ids=lambda path: path.stem)
def test_route_example_loads_and_matches_its_file_name(path: Path) -> None:
    assert parse_route(load_yaml(path)).name == path.stem


def test_examples_have_a_few_routes_of_each_type() -> None:
    types: list[str] = [parse_route(load_yaml(path)).type for path in ROUTE_FILES]
    assert {kind: types.count(kind) >= 2 for kind in ("http", "passthrough", "tcp")} == {
        "http": True,
        "passthrough": True,
        "tcp": True,
    }


def test_examples_agree_with_each_other() -> None:
    config = Config.model_validate(load_yaml(DATA / "config" / "config.yaml"))
    secrets = Secrets.model_validate(load_yaml(DATA / "secrets" / "secrets.yaml"))
    routes = [parse_route(load_yaml(path)) for path in ROUTE_FILES]

    assert {domain.credentials for domain in config.domains} <= set(secrets.dns_credentials)
    zones = tuple(f".{domain.zone}" for domain in config.domains)
    hosts = [host for route in routes if route.type != "tcp" for host in route.hosts]
    assert all(host.endswith(zones) for host in hosts)
    assert len(set(hosts)) == len(hosts)
    basic = {r.name for r in routes if isinstance(r, HttpRoute) and r.auth.type == "basic"}
    assert basic == set(secrets.basic_auth)


def test_seeded_header_value_keeps_its_quotes() -> None:
    route = parse_route(load_yaml(ROUTES / "jellyfin.yaml"))
    assert isinstance(route, HttpRoute)
    assert route.response_headers["Content-Security-Policy"] == "default-src 'self'"


def test_seeded_route_can_leave_proxied_unset() -> None:
    route = parse_route(load_yaml(ROUTES / "whoami.yaml"))
    assert isinstance(route, HttpRoute)
    assert route.cloudflare.proxied is None


def validate_config(data: object) -> None:
    Config.model_validate(data)


def validate_route(data: object) -> None:
    parse_route(data)


def validate_secrets(data: object) -> None:
    Secrets.model_validate(data)


INVALID_CASES = [
    (validate_route, "route-name-newline", ("http", "name")),
    (validate_route, "route-name-quote", ("http", "name")),
    (validate_route, "route-host-newline", ("http", "hosts", 0)),
    (validate_route, "route-host-quote", ("http", "hosts", 0)),
    (
        validate_route,
        "route-header-name-newline",
        ("http", "request_headers", "X-Evil\nX-Other", "[key]"),
    ),
    (validate_route, "route-header-name-quote", ("http", "request_headers", 'X-"Evil"', "[key]")),
    (validate_route, "route-header-value-newline", ("http", "request_headers", "X-Test")),
    (validate_route, "route-upstream-newline", ("http", "upstreams", 0, "address")),
    (validate_route, "route-no-upstreams", ("http", "upstreams")),
    (validate_route, "route-passthrough-with-auth", ("passthrough", "auth")),
    (validate_route, "route-tcp-without-port", ("tcp", "listen_port")),
    (validate_route, "route-tcp-port-out-of-range", ("tcp", "listen_port")),
    (validate_config, "config-bad-cidr", ("network", "lan_cidrs", 0)),
    (validate_config, "config-bad-zone", ("domains", 0, "zone")),
    (validate_config, "config-unknown-key", ("network", "http_prot")),
    (validate_secrets, "secrets-bad-basic-auth-hash", ("basic_auth", "files", "alice")),
    (validate_secrets, "secrets-short-session-key", ("session_key",)),
]


@pytest.mark.parametrize(
    ("validate", "name", "field"), INVALID_CASES, ids=[c[1] for c in INVALID_CASES]
)
def test_invalid_example_fails_on_its_field(
    validate: Any, name: str, field: tuple[str | int, ...]
) -> None:
    with pytest.raises(ValidationError) as info:
        validate(load_yaml(INVALID / f"{name}.yaml"))
    assert [error["loc"] for error in info.value.errors()] == [field]


def without_attack_characters(node: Any) -> Any:
    if isinstance(node, dict):
        return {
            without_attack_characters(key): without_attack_characters(item)
            for key, item in node.items()
        }
    if isinstance(node, list):
        return [without_attack_characters(item) for item in node]
    if isinstance(node, str):
        return node.replace("\n", "").replace('"', "")
    return node


ATTACK_CASES = [
    case[:2]
    for case in INVALID_CASES
    if case[1].endswith(("-newline", "-quote"))
    or case[1] in {"config-bad-zone", "secrets-bad-basic-auth-hash"}
]


@pytest.mark.parametrize(("validate", "name"), ATTACK_CASES, ids=[c[1] for c in ATTACK_CASES])
def test_attack_example_fails_only_because_of_its_attack_character(
    validate: Any, name: str
) -> None:
    validate(without_attack_characters(load_yaml(INVALID / f"{name}.yaml")))


def test_every_invalid_example_is_covered() -> None:
    assert {path.stem for path in INVALID.glob("*.yaml")} == {case[1] for case in INVALID_CASES}
