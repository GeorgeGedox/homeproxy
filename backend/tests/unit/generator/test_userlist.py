"""Basic auth rules that the golden files do not show on their own."""

import stat
from pathlib import Path

import pytest
from pydantic import SecretStr
from ruamel.yaml import YAML

from homeproxy.generator import GeneratorError, RouteError, render, write_release
from homeproxy.models import Config, HttpRoute, parse_route

GOLDEN = Path(__file__).parents[2] / "golden"
HASH = (
    "$6$bobsalt$"
    "0ayWbOvdpplHpxhb32EQ6IlDPHCjv7U1gh18r88FYW2UYyRurowntIrZYaeBgHKXgxmOEl1wLBOVgEHs0XiPz/"
)
INJECTED = "\n    user evil insecure-password evil"


def make_config() -> Config:
    return Config.model_validate(YAML(typ="rt").load(GOLDEN / "base-config.yaml"))


def locked_route() -> HttpRoute:
    route = parse_route(
        {
            "version": 1,
            "name": "app",
            "type": "http",
            "hosts": ["app.example.com"],
            "exposure": "public",
            "upstreams": [{"address": "192.168.1.20:8080"}],
            "auth": {"type": "basic"},
        }
    )
    assert isinstance(route, HttpRoute)
    return route


def test_basic_auth_route_without_users_is_refused() -> None:
    with pytest.raises(RouteError) as error:
        render(make_config(), [locked_route()], basic_auth={"app": {}})
    assert str(error.value).startswith("route 'app': auth: ")


def test_userlist_file_is_readable_only_by_its_owner(tmp_path: Path) -> None:
    release = render(make_config(), [locked_route()], basic_auth={"app": {"bob": SecretStr(HASH)}})
    write_release(release, tmp_path / "release")
    assert stat.S_IMODE((tmp_path / "release/conf.d/15-userlists.cfg").stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "release/conf.d/20-backends.cfg").stat().st_mode) == 0o644


@pytest.mark.parametrize(
    ("user", "hash_value"),
    [
        (f"bob{INJECTED}", HASH),
        ("bob password x", HASH),
        ("bob#", HASH),
        ('"bob', HASH),
        ("-bob", HASH),
        ("bob", HASH + INJECTED),
        ("bob", HASH + " # x"),
        ("bob", "$5$bobsalt$" + "a" * 43),
        ("bob", HASH.replace("$6$", "$6$rounds=50001$")),
        ("bob", "evil-plain-text"),
        ("bob", '"' + HASH),
    ],
    ids=[
        "user-newline",
        "user-space",
        "user-comment",
        "user-quote",
        "user-option",
        "hash-newline",
        "hash-space",
        "hash-other-format",
        "hash-too-many-rounds",
        "hash-plain-text",
        "hash-quote",
    ],
)
def test_value_that_skipped_its_model_never_reaches_the_userlist(
    user: str, hash_value: str
) -> None:
    """`Secrets.model_construct` does not validate, so the filters must hold on their own."""
    with pytest.raises(GeneratorError) as error:
        render(make_config(), [locked_route()], basic_auth={"app": {user: SecretStr(hash_value)}})
    assert "evil" not in str(error.value)
    assert HASH not in str(error.value)
