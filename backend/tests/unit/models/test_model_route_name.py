"""The name the server gives a route created without one."""

from typing import Any

import pytest
from pydantic import TypeAdapter

from homeproxy.models import parse_route
from homeproxy.models.fields import Name
from homeproxy.models.route_name import new_route_name

# Cut at 63 characters, the slug ends on the hyphen between these two labels.
LONG_HOST = f"{'a' * 62}.{'b' * 63}.example.com"


def route(type_: str = "http", **fields: Any) -> Any:
    base: dict[str, Any] = {"version": 1, "name": "x", "type": type_, "exposure": "lan"}
    if type_ != "redirect":
        base["upstreams"] = [{"address": "192.168.1.10:8080"}]
    return parse_route(base | fields)


@pytest.mark.parametrize(
    ("draft", "name"),
    [
        (route(hosts=["app.example.com", "b.example.com"]), "app-example-com"),
        (route(hosts=["example.com"], path_prefix="/API_v2.1"), "example-com-api-v2-1"),
        (route("passthrough", hosts=["pve.example.com"]), "pve-example-com"),
        (route("tcp", listen_port=25565), "tcp-25565"),
        (route(hosts=[LONG_HOST]), "a" * 62),
    ],
)
def test_a_name_comes_from_the_first_host_and_path_or_the_port(draft: Any, name: str) -> None:
    """Characters a name cannot hold become hyphens, and a long host is cut to a valid name."""
    made = new_route_name(draft, lambda _: False)

    assert made == name
    assert TypeAdapter(Name).validate_python(made) == made


@pytest.mark.parametrize(
    ("host", "taken", "name"),
    [
        ("app.example.com", {"app-example-com", "app-example-com-2"}, "app-example-com-3"),
        (LONG_HOST, {"a" * 62}, "a" * 61 + "-2"),
    ],
)
def test_a_taken_name_gets_a_number_and_stays_valid(host: str, taken: set[str], name: str) -> None:
    """The number replaces the end of a name at full length rather than make it too long."""
    made = new_route_name(route(hosts=[host]), taken.__contains__)

    assert made == name
    assert TypeAdapter(Name).validate_python(made) == made
