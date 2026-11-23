"""Route validation, with attack values for each field that can reach generated HAProxy files."""

from collections.abc import Callable
from typing import Any

import pytest
from pydantic import ValidationError
from ruamel.yaml import YAML

from homeproxy.models import HttpRoute, PassthroughRoute, RedirectRoute, TcpRoute, parse_route

# Each one could end a directive, start a new one or break out of a quoted argument.
HOSTILE_CHARACTERS = [
    "\n",
    "\r",
    "\r\n",
    '"',
    "'",
    "\x00",
    "\x7f",
    " ",
    "\t",
    "#",
    "\\",
    "\u2028",
    "\u0085",
]


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


def passthrough_route(**changes: Any) -> dict[str, Any]:
    return http_route(type="passthrough", **changes)


def tcp_route(**changes: Any) -> dict[str, Any]:
    route = http_route(type="tcp", listen_port=2222, **changes)
    if "hosts" not in changes:
        del route["hosts"]
    return route


def redirect_route(**changes: Any) -> dict[str, Any]:
    route = http_route(type="redirect", **{"redirect": {"to": "https://example.com"}, **changes})
    if "upstreams" not in changes:
        del route["upstreams"]
    return route


def error_locations(data: dict[str, Any]) -> list[tuple[str | int, ...]]:
    with pytest.raises(ValidationError) as info:
        parse_route(data)
    return [error["loc"] for error in info.value.errors()]


FIELDS: list[tuple[str, str, Callable[[str], dict[str, Any]], tuple[str | int, ...]]] = [
    ("name", "app", lambda value: http_route(name=value), ("http", "name")),
    ("host", "app.example.com", lambda value: http_route(hosts=[value]), ("http", "hosts", 0)),
    (
        "passthrough host",
        "app.example.com",
        lambda value: passthrough_route(hosts=[value]),
        ("passthrough", "hosts", 0),
    ),
    (
        "upstream",
        "192.168.1.20:8080",
        lambda value: http_route(upstreams=[{"address": value}]),
        ("http", "upstreams", 0, "address"),
    ),
    (
        "health path",
        "/health",
        lambda value: http_route(health_check={"path": value}),
        ("http", "health_check", "path"),
    ),
    (
        "path prefix",
        "/api",
        lambda value: http_route(path_prefix=value),
        ("http", "path_prefix"),
    ),
    (
        "interval",
        "10s",
        lambda value: http_route(health_check={"path": "/", "interval": value}),
        ("http", "health_check", "interval"),
    ),
    (
        "dns target",
        "haproxy",
        lambda value: http_route(dns={"target": value}),
        ("http", "dns", "target"),
    ),
    (
        "redirect target",
        "https://example.com/a",
        lambda value: redirect_route(redirect={"to": value}),
        ("redirect", "redirect", "to"),
    ),
    (
        "country",
        "AQ",
        lambda value: http_route(geoip={"block_countries": [value]}),
        ("http", "geoip", "block_countries", 0),
    ),
]


@pytest.mark.parametrize("character", HOSTILE_CHARACTERS, ids=ascii)
@pytest.mark.parametrize(("field", "good", "build", "location"), FIELDS, ids=[f[0] for f in FIELDS])
def test_hostile_character_is_rejected(
    field: str,
    good: str,
    build: Callable[[str], dict[str, Any]],
    location: tuple[str | int, ...],
    character: str,
) -> None:
    parse_route(build(good))
    for value in (f"{good}{character}", f"{character}{good}", f"{good}{character}{good}"):
        assert error_locations(build(value)) == [location], field


HEADER_FIELDS = ("request_headers", "response_headers")


@pytest.mark.parametrize("field", HEADER_FIELDS)
@pytest.mark.parametrize("character", HOSTILE_CHARACTERS, ids=ascii)
def test_hostile_character_in_header_name_is_rejected(character: str, field: str) -> None:
    name = f"X-Test{character}"
    assert error_locations(http_route(**{field: {name: "ok"}})) == [("http", field, name, "[key]")]


@pytest.mark.parametrize(
    "character",
    ["\n", "\r", "\x00", "\x1f", "\x7f", "\t", "\u2028", "\u0085", "é"],
    ids=ascii,
)
def test_control_or_non_ascii_character_in_header_value_is_rejected(character: str) -> None:
    location = ("http", "response_headers", "X-Test")
    assert error_locations(http_route(response_headers={"X-Test": f"a{character}b"})) == [location]
    assert error_locations(http_route(response_headers={"X-Test": f"ab{character}"})) == [location]


@pytest.mark.parametrize("field", HEADER_FIELDS)
def test_header_injection_payload_is_rejected(field: str) -> None:
    payload = 'x"\n    http-request deny\n    http-request set-header X-Evil "y'
    assert error_locations(http_route(**{field: {"X-Test": payload}})) == [
        ("http", field, "X-Test")
    ]


@pytest.mark.parametrize("value", ["default-src 'self'", 'say "hi"', "it's", '"', "'"], ids=ascii)
def test_quotes_in_header_value_are_accepted(value: str) -> None:
    route = parse_route(http_route(response_headers={"X-Test": value}))
    assert isinstance(route, HttpRoute)
    assert route.response_headers == {"X-Test": value}


def test_empty_header_value_is_rejected() -> None:
    assert error_locations(http_route(response_headers={"X-Test": ""})) == [
        ("http", "response_headers", "X-Test")
    ]


def test_header_value_length_is_limited() -> None:
    parse_route(http_route(response_headers={"X-Test": "a" * 1024}))
    location = ("http", "response_headers", "X-Test")
    assert error_locations(http_route(response_headers={"X-Test": "a" * 1025})) == [location]


def test_header_name_length_is_limited() -> None:
    parse_route(http_route(response_headers={"X" * 128: "ok"}))
    name = "X" * 129
    assert error_locations(http_route(response_headers={name: "ok"})) == [
        ("http", "response_headers", name, "[key]")
    ]


def test_health_path_length_is_limited() -> None:
    good = "/" + "a" * 255
    parse_route(http_route(health_check={"path": good}))
    location = ("http", "health_check", "path")
    assert error_locations(http_route(health_check={"path": good + "a"})) == [location]


def test_upstream_length_is_limited() -> None:
    labels = ".".join(["a" * 63] * 5)
    location = ("http", "upstreams", 0, "address")
    assert error_locations(http_route(upstreams=[{"address": f"{labels}:80"}])) == [location]


def test_ordinary_header_value_is_accepted() -> None:
    value = "max-age=31536000; includeSubDomains, a/b (c) [d] #e %f"
    route = parse_route(http_route(response_headers={"Strict-Transport-Security": value}))
    assert isinstance(route, HttpRoute)
    assert route.response_headers == {"Strict-Transport-Security": value}


@pytest.mark.parametrize(
    "name",
    ["", "-app", "App", "a_b", "a.b", "a" * 64, "app\n", "jellyfin\nbackend evil"],
    ids=ascii,
)
def test_bad_name_is_rejected(name: str) -> None:
    assert error_locations(http_route(name=name)) == [("http", "name")]


@pytest.mark.parametrize("name", ["a", "0", "a-b", "a" * 63])
def test_good_name_is_accepted(name: str) -> None:
    assert parse_route(http_route(name=name)).name == name


@pytest.mark.parametrize(
    "host",
    [
        "example",
        "EXAMPLE.com",
        "*.example.com",
        "-a.example.com",
        "a-.example.com",
        "a..example.com",
        ".example.com",
        "example.com.",
        "a_b.example.com",
        "exämple.com",
        "192.168.1.10",
        "999.1.1.1",
        "0x7f.0x1",
        "app.0x1",
        "app.123",
        f"{'a' * 64}.example.com",
        ".".join(["a" * 63] * 4 + ["example"]),
    ],
)
def test_bad_host_is_rejected(host: str) -> None:
    assert error_locations(http_route(hosts=[host])) == [("http", "hosts", 0)]


@pytest.mark.parametrize(
    "prefix", ["/", "/api", "/api/v2", "/.well-known", "/..x", "/...", "/v1.2_x-y", "/A"]
)
def test_good_path_prefix_is_accepted(prefix: str) -> None:
    route = parse_route(http_route(path_prefix=prefix))
    assert isinstance(route, HttpRoute)
    assert route.path_prefix == prefix


@pytest.mark.parametrize(
    "prefix",
    [
        "",
        "api",
        "/api/",
        "//api",
        "/api//v2",
        "/.",
        "/..",
        "/api/../admin",
        "/a%20b",
        "/%[req.hdr(cookie)]",
        "/api?x=1",
        "/api$",
        "/~user",
        "/é",
        "/" + "a" * 256,
    ],
    ids=ascii,
)
def test_bad_path_prefix_is_rejected(prefix: str) -> None:
    assert error_locations(http_route(path_prefix=prefix)) == [("http", "path_prefix")]


def test_empty_or_repeated_hosts_are_rejected() -> None:
    assert error_locations(http_route(hosts=[])) == [("http", "hosts")]
    host = "app.example.com"
    assert error_locations(http_route(hosts=[host, host])) == [("http", "hosts")]


@pytest.mark.parametrize(
    "address", ["192.168.1.20:8096", "whoami-a:80", "app.example.com:443", "192.168.1.20:65535"]
)
def test_good_upstream_is_accepted(address: str) -> None:
    parse_route(http_route(upstreams=[{"address": address}]))


@pytest.mark.parametrize(
    "address",
    [
        "192.168.1.20",
        "192.168.1.20:0",
        "192.168.1.20:65536",
        "192.168.1.20:80:80",
        "192.168.1.20:00080",
        "192.168.1.20:080",
        "0x7f000001:8443",
        "0300.0250.0.0x1:8443",
        "0177.0.0.1:80",
        "127.1:80",
        "app.0x1:80",
        "0x:80",
        "999.1.1.1:80",
        "1234:80",
        "[fd00::1]:80",
        "http://192.168.1.20:80",
        "App:80",
    ],
)
def test_bad_upstream_is_rejected(address: str) -> None:
    location = ("http", "upstreams", 0, "address")
    assert error_locations(http_route(upstreams=[{"address": address}])) == [location]


def test_name_that_only_starts_with_a_number_is_accepted() -> None:
    hosts = ["0x1.example.com", "123.example.com"]
    route = parse_route(http_route(hosts=hosts, upstreams=[{"address": "0x1.example.com:80"}]))
    assert isinstance(route, HttpRoute)
    assert route.hosts == hosts


@pytest.mark.parametrize("target", ["256.1.1.1", "auto", "app.example.com", "fd00::1"])
def test_bad_dns_target_is_rejected(target: str) -> None:
    assert error_locations(http_route(dns={"target": target})) == [("http", "dns", "target")]


def test_type_selects_the_model() -> None:
    assert isinstance(parse_route(http_route()), HttpRoute)
    assert isinstance(parse_route(passthrough_route()), PassthroughRoute)
    assert isinstance(parse_route(tcp_route()), TcpRoute)
    assert isinstance(parse_route(redirect_route()), RedirectRoute)


@pytest.mark.parametrize(
    "target",
    [
        "https://example.com",
        "http://192.168.1.10:8080/",
        "https://example.com/path/a-b_c~d.e?x=1&y=%20,z;w@v:u+t!s",
    ],
)
def test_good_redirect_target_is_accepted(target: str) -> None:
    parse_route(redirect_route(redirect={"to": target}))


@pytest.mark.parametrize(
    "target",
    [
        "example.com",
        "ftp://example.com",
        "javascript:alert(1)",
        "https://",
        "https://Example.com",
        "https://example.com?x=1",
        "https://user@example.com",
        "https://example.com:99999",
        "https://999.1.1.1",
        "https://example.com/%[req.hdr(cookie)]",
        "https://example.com/a|b",
        "https://example.com/" + "a" * 2048,
    ],
)
def test_bad_redirect_target_is_rejected(target: str) -> None:
    errors = error_locations(redirect_route(redirect={"to": target}))
    assert errors == [("redirect", "redirect", "to")]


@pytest.mark.parametrize("target", ["https://app.example.com/x", "http://app.example.com:8080"])
def test_redirect_to_its_own_host_is_rejected(target: str) -> None:
    errors = error_locations(redirect_route(redirect={"to": target}))
    assert errors == [("redirect", "redirect", "to")]


@pytest.mark.parametrize("code", [300, 303, "301", 301.0, True], ids=repr)
def test_bad_redirect_code_is_rejected(code: object) -> None:
    data = redirect_route(redirect={"to": "https://example.com", "code": code})
    assert error_locations(data) == [("redirect", "redirect", "code")]


def test_a_redirect_that_keeps_the_path_takes_no_query_in_its_target() -> None:
    """HAProxy appends the request's path and query, so `/?ref=www` would become `/?ref=www/x?y`."""
    data = redirect_route(redirect={"to": "https://example.com/?ref=www", "keep_path": True})
    assert error_locations(data) == [("redirect", "redirect", "to")]
    parse_route(redirect_route(redirect={"to": "https://example.com/?ref=www"}))


def test_redirect_defaults_to_301_without_the_path() -> None:
    route = parse_route(redirect_route())
    assert isinstance(route, RedirectRoute)
    assert (route.redirect.code, route.redirect.keep_path) == (301, False)


@pytest.mark.parametrize(
    "field",
    [
        {"upstreams": [{"address": "192.168.1.20:8080"}]},
        {"auth": {"type": "none"}},
        {"headers": {}},
        {"listen_port": 2222},
    ],
    ids=lambda field: next(iter(field)),
)
def test_redirect_rejects_fields_of_other_types(field: dict[str, Any]) -> None:
    assert error_locations(redirect_route(**field)) == [("redirect", next(iter(field)))]


@pytest.mark.parametrize(
    "data", [{"name": "app"}, {"name": "app", "type": "udp"}], ids=["missing", "unknown"]
)
def test_missing_or_unknown_type_names_the_type_field(data: dict[str, str]) -> None:
    with pytest.raises(ValidationError) as info:
        parse_route(data)
    assert "'type'" in info.value.errors()[0]["msg"]


@pytest.mark.parametrize(
    "field",
    [
        {"auth": {"type": "basic"}},
        {"headers": {"X-Test": "ok"}},
        {"websocket": True},
        {"tls_upstream": {"enabled": True}},
        {"health_check": {"path": "/"}},
        {"listen_port": 2222},
    ],
    ids=lambda field: next(iter(field)),
)
def test_passthrough_rejects_fields_of_other_types(field: dict[str, Any]) -> None:
    assert error_locations(passthrough_route(**field)) == [("passthrough", next(iter(field)))]


@pytest.mark.parametrize(
    "field",
    [
        {"hosts": ["app.example.com"]},
        {"auth": {"type": "none"}},
        {"headers": {}},
        {"cloudflare": {"dns": True}},
        {"dns": {"target": "haproxy"}},
    ],
    ids=lambda field: next(iter(field)),
)
def test_tcp_rejects_fields_of_other_types(field: dict[str, Any]) -> None:
    assert error_locations(tcp_route(**field)) == [("tcp", next(iter(field)))]


def test_http_rejects_listen_port() -> None:
    assert error_locations(http_route(listen_port=2222)) == [("http", "listen_port")]


@pytest.mark.parametrize("port", [0, 65536, -1, "2222", True, 22.5], ids=repr)
def test_bad_listen_port_is_rejected(port: object) -> None:
    assert error_locations(tcp_route() | {"listen_port": port}) == [("tcp", "listen_port")]


@pytest.mark.parametrize(
    ("changes", "location"),
    [
        ({"version": 2}, ("http", "version")),
        ({"version": True}, ("http", "version")),
        ({"version": 1.0}, ("http", "version")),
        ({"version": "1"}, ("http", "version")),
        ({"exposure": "wan"}, ("http", "exposure")),
        ({"enabled": "yes"}, ("http", "enabled")),
        ({"websocket": 1}, ("http", "websocket")),
        ({"auth": {"type": "forward_auth"}}, ("http", "auth", "type")),
        ({"auth": {"type": "basic", "password": "x"}}, ("http", "auth", "password")),
        ({"upstreams": []}, ("http", "upstreams")),
        ({"host": "app.example.com"}, ("http", "host")),
    ],
    ids=repr,
)
def test_wrong_value_fails_on_its_field(changes: dict[str, Any], location: tuple[str, ...]) -> None:
    assert error_locations(http_route(**changes)) == [location]


def test_defaults_are_filled_in() -> None:
    route = parse_route(http_route())
    assert isinstance(route, HttpRoute)
    assert route.health_check is None
    assert route.tls_upstream.enabled is False
    assert route.tls_upstream.verify is True
    assert route.websocket is False
    assert route.auth.type == "none"
    assert route.response_headers == {}
    assert (route.path_prefix, route.strip_prefix) == ("/", False)
    assert route.cloudflare.dns is False
    assert route.dns.target == "haproxy"
    assert route.enabled is True


ROUTE_BUILDERS = {
    "http": http_route,
    "passthrough": passthrough_route,
    "tcp": tcp_route,
    "redirect": redirect_route,
}
REQUIRED_KEYS = [
    ("http", "version"),
    ("http", "name"),
    ("http", "hosts"),
    ("http", "exposure"),
    ("http", "upstreams"),
    ("passthrough", "version"),
    ("passthrough", "name"),
    ("passthrough", "hosts"),
    ("passthrough", "exposure"),
    ("passthrough", "upstreams"),
    ("tcp", "version"),
    ("tcp", "name"),
    ("tcp", "listen_port"),
    ("tcp", "exposure"),
    ("tcp", "upstreams"),
    ("redirect", "hosts"),
    ("redirect", "exposure"),
    ("redirect", "redirect"),
]


@pytest.mark.parametrize(("kind", "key"), REQUIRED_KEYS)
def test_missing_required_key_fails(kind: str, key: str) -> None:
    data = ROUTE_BUILDERS[kind]()
    parse_route(data)
    del data[key]
    assert error_locations(data) == [(kind, key)]


@pytest.mark.parametrize("kind", ["http", "passthrough", "tcp"])
def test_route_needs_at_least_one_upstream(kind: str) -> None:
    assert error_locations(ROUTE_BUILDERS[kind](upstreams=[])) == [(kind, "upstreams")]


def test_unset_proxied_on_http_means_the_zone_default() -> None:
    route = parse_route(http_route(cloudflare={"dns": True}))
    assert isinstance(route, HttpRoute)
    assert route.cloudflare.dns is True
    assert route.cloudflare.proxied is None


def test_absent_cloudflare_on_http_leaves_proxied_unset() -> None:
    route = parse_route(http_route())
    assert isinstance(route, HttpRoute)
    assert route.cloudflare.proxied is None


@pytest.mark.parametrize("value", [True, False])
def test_explicit_proxied_on_http_is_kept(value: bool) -> None:
    route = parse_route(http_route(cloudflare={"dns": True, "proxied": value}))
    assert isinstance(route, HttpRoute)
    assert route.cloudflare.proxied is value


def test_dump_keeps_unset_proxied_unset() -> None:
    route = parse_route(http_route(cloudflare={"dns": True}))
    assert route.model_dump(exclude_unset=True)["cloudflare"] == {"dns": True}
    dumped = route.model_dump()
    assert dumped["cloudflare"]["proxied"] is None
    again = parse_route(dumped)
    assert isinstance(again, HttpRoute)
    assert again.cloudflare.proxied is None


def test_passthrough_proxied_defaults_to_off() -> None:
    route = parse_route(passthrough_route(cloudflare={"dns": True}))
    assert isinstance(route, PassthroughRoute)
    assert route.cloudflare.proxied is False


def test_passthrough_proxied_cannot_be_null() -> None:
    data = passthrough_route(cloudflare={"proxied": None})
    assert error_locations(data) == [("passthrough", "cloudflare", "proxied")]


def test_validated_route_is_frozen() -> None:
    route = parse_route(http_route())
    with pytest.raises(ValidationError):
        route.name = "a\nb"
    assert route.name == "app"


@pytest.mark.parametrize("field", ["version", "listen_port"])
def test_anchored_true_is_not_an_integer(field: str) -> None:
    text = "version: 1\nname: app\ntype: tcp\nlisten_port: 2222\nexposure: lan\n"
    text += "upstreams:\n  - address: 192.168.1.20:22\n"
    parse_route(YAML(typ="rt").load(text))
    hostile = text.replace(f"{field}: ", f"{field}: &anchor true #", 1)
    assert error_locations(YAML(typ="rt").load(hostile)) == [("tcp", field)]
