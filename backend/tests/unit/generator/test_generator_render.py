"""Rendering rules that the golden files do not show on their own."""

import re
import stat
from pathlib import Path
from typing import Any

import pytest

from homeproxy.generator import GeneratorError, RouteError, render, write_release
from homeproxy.generator.render import ENVIRONMENT
from homeproxy.models import Config, HttpRoute, Route, parse_route
from homeproxy.models.route import Upstream

HEADER_VALUE = "attack-value-in-message"


def make_config(**network: Any) -> Config:
    return Config.model_validate(
        {
            "version": 1,
            "dashboard": {
                "listen": "192.168.1.10:8443",
                "password_hash": "$argon2id$v=19$m=65536,t=3,p=4$ZmFrZS1zYWx0$ZmFrZS1oYXNo",
            },
            "network": {
                "lan_cidrs": ["192.168.1.0/24"],
                "haproxy_lan_ip": "192.168.1.10",
                **network,
            },
            "dns_server": {"listen": "192.168.1.10:5335"},
        }
    )


def make_route(name: str = "app", **fields: Any) -> HttpRoute:
    route = parse_route(
        {
            "version": 1,
            "name": name,
            "type": "http",
            "hosts": [f"{name}.example.com"],
            "exposure": "public",
            "upstreams": [{"address": "192.168.1.20:8080"}],
            **fields,
        }
    )
    assert isinstance(route, HttpRoute)
    return route


def global_section() -> str:
    main = render(make_config(), []).files["conf.d/10-haproxy.cfg"]
    return main.partition("global\n")[2].partition("\ndefaults\n")[0]


def test_global_section_runs_unprivileged_with_the_contract_socket() -> None:
    section = global_section()
    assert re.search(r"^\s*(user|group|uid|gid)\b", section, re.MULTILINE) is None
    assert "stats socket /run/haproxy/admin.sock " in section


def test_global_section_keeps_state_and_limits_old_workers() -> None:
    section = global_section()
    assert "default-path parent\n" in section
    assert "server-state-file /data/state/haproxy-server-state\n" in section
    assert re.search(r"^\s*hard-stop-after \d+m$", section, re.MULTILINE) is not None


def test_servers_are_named_after_their_address_and_resolve_on_every_start() -> None:
    """The state file matches servers by name. `last` would keep an old address from it."""
    route = make_route(upstreams=[{"address": "192.168.1.20:8080"}, {"address": "app-b:80"}])
    files = render(make_config(), [route]).files
    backends = files["conf.d/20-backends.cfg"]
    assert "    server 192.168.1.20:8080 192.168.1.20:8080 check" in backends
    assert "    server app-b:80 app-b:80 check" in backends
    assert "    default-server init-addr libc,none\n" in files["conf.d/10-haproxy.cfg"]


def test_same_upstream_twice_is_rejected() -> None:
    route = make_route(upstreams=[{"address": "192.168.1.20:80"}, {"address": "192.168.1.20:80"}])
    with pytest.raises(RouteError) as error:
        render(make_config(), [route])
    assert str(error.value).startswith("route 'app': upstreams: ")


def test_includes_are_copied_unchanged_after_the_generated_files() -> None:
    text = "# Grüße\nbackend legacy\n    server legacy 192.168.1.50:8080\n"
    files = render(make_config(), [], {"zz.last": text, "a-first": ""}).files
    assert files["conf.d/90-include-zz.last.cfg"] == text
    assert files["conf.d/90-include-a-first.cfg"] == ""
    conf_files = sorted(path for path in files if path.startswith("conf.d/"))
    assert conf_files[-2:] == ["conf.d/90-include-a-first.cfg", "conf.d/90-include-zz.last.cfg"]


@pytest.mark.parametrize("name", ["", "a/b", "../a", "a b", "a\nb", "-a", "a%b"])
def test_include_name_that_is_not_one_plain_word_is_rejected(name: str) -> None:
    with pytest.raises(GeneratorError):
        render(make_config(), [], {name: ""})


def test_include_text_is_written_as_utf8(tmp_path: Path) -> None:
    text = "# Grüße\n"
    write_release(render(make_config(), [], {"note": text}), tmp_path / "release")
    path = tmp_path / "release" / "conf.d" / "90-include-note.cfg"
    assert path.read_bytes() == text.encode("utf-8")


def test_defaults_set_every_slow_client_timeout() -> None:
    main = render(make_config(), []).files["conf.d/10-haproxy.cfg"]
    for name in ("http-request", "client", "http-keep-alive", "connect", "server"):
        assert re.search(rf"^\s*timeout {name} \d+[a-z]+$", main, re.MULTILINE), name
    assert "load-server-state-from-file global\n" in main


def test_map_and_list_paths_are_relative() -> None:
    main = render(make_config(), []).files["conf.d/10-haproxy.cfg"]
    assert re.findall(r"[\w/.-]+\.(?:map|lst)", main) == [
        "hosts.map",
        "paths.map",
        "cloudflare.lst",
        "proxied-only.map",
        "country.map",
        "plain-http.map",
        "redirect.map",
        "country-block.map",
        "lan.lst",
        "lan-only.map",
        "lan.lst",
        "proxied-only.map",
        "lan.lst",
        "hsts.map",
    ]


def test_output_does_not_depend_on_input_order() -> None:
    config = make_config()
    one = render(
        config,
        [
            make_route("first", hosts=["b.example.com", "a.example.com"]),
            make_route("second", response_headers={"X-B": "b", "x-a": "a"}),
            make_route("third", exposure="lan"),
        ],
    )
    two = render(
        config,
        [
            make_route("third", exposure="lan"),
            make_route("second", response_headers={"x-a": "a", "X-B": "b"}),
            make_route("first", hosts=["a.example.com", "b.example.com"]),
        ],
    )
    assert one == two


PASSTHROUGH = {"type": "passthrough", "upstreams": [{"address": "192.168.1.20:443"}]}
TCP = {"type": "tcp", "listen_port": 2222, "upstreams": [{"address": "192.168.1.20:22"}]}


def any_route(fields: dict[str, Any], exposure: str) -> Route:
    route = {"version": 1, "name": "app", "hosts": ["app.example.com"], "exposure": exposure}
    route.update(fields)
    if fields.get("type") == "tcp":
        del route["hosts"]
    return parse_route(route)


@pytest.mark.parametrize(
    ("fields", "changed"),
    [
        ({"type": "http", "upstreams": [{"address": "192.168.1.20:80"}]}, {"hsts.map"}),
        (PASSTHROUGH, set()),
        (TCP, set()),
    ],
    ids=["http", "passthrough", "tcp"],
)
def test_switching_exposure_changes_only_the_maps(
    fields: dict[str, Any], changed: set[str]
) -> None:
    """The apply pipeline makes this switch through the Runtime API, without a reload."""
    config = make_config()
    public = render(config, [any_route(fields, "public")]).files
    lan = render(config, [any_route(fields, "lan")]).files
    assert {path for path in public if public[path] != lan[path]} == {"lan-only.map", *changed}
    assert public["lan-only.map"] == ""
    assert lan["lan-only.map"] == "route-app 1\n"
    for path in changed:
        assert (public[path], lan[path]) == ("route-app 1\n", "")


def test_backend_names_have_a_prefix() -> None:
    files = render(
        make_config(), [make_route("global"), make_route("stats-", hosts=["stats.example.com"])]
    ).files
    assert "backend route-global\n" in files["conf.d/20-backends.cfg"]
    assert "backend route-stats-\n" in files["conf.d/20-backends.cfg"]
    assert "global.example.com route-global\n" in files["hosts.map"]


FRAMING_HEADERS = [
    "Connection",
    "Content-Length",
    "Keep-Alive",
    "Proxy-Connection",
    "TE",
    "Trailer",
    "Transfer-Encoding",
    "transfer-encoding",
    "Upgrade",
]


@pytest.mark.parametrize(
    ("field", "name"),
    [
        *(("response_headers", name) for name in FRAMING_HEADERS),
        ("response_headers", "Host"),
        ("response_headers", "HOST"),
        ("response_headers", "CF-Connecting-IP"),
        ("response_headers", "cf-connecting-ip"),
        *(("request_headers", name) for name in FRAMING_HEADERS),
        ("request_headers", "X-Forwarded-For"),
        ("request_headers", "x-forwarded-proto"),
        ("request_headers", "CF-Connecting-IP"),
    ],
)
def test_header_that_breaks_a_message_or_hides_the_real_client_is_rejected(
    field: str, name: str
) -> None:
    route = make_route("app", **{field: {name: HEADER_VALUE}})
    with pytest.raises(RouteError) as error:
        render(make_config(), [route])
    assert str(error.value).startswith(f"route 'app': {field}: ")
    assert HEADER_VALUE not in str(error.value)


@pytest.mark.parametrize("field", ["request_headers", "response_headers"])
def test_header_names_that_differ_only_in_case_are_rejected(field: str) -> None:
    route = make_route("app", **{field: {"X-Same": HEADER_VALUE, "x-same": HEADER_VALUE}})
    with pytest.raises(RouteError) as error:
        render(make_config(), [route])
    assert str(error.value).startswith(f"route 'app': {field}: ")
    assert HEADER_VALUE not in str(error.value)


def test_other_headers_are_set_in_their_direction() -> None:
    route = make_route(
        request_headers={"Host": "app.internal", "Authorization": "Bearer t"},
        response_headers={"Set-Cookie": "a=b", "Strict-Transport-Security": "max-age=1"},
    )
    backends = render(make_config(), [route]).files["conf.d/20-backends.cfg"]
    assert 'http-request set-header Host "app.internal"\n' in backends
    assert 'http-request set-header Authorization "Bearer t"\n' in backends
    assert 'http-response set-header Set-Cookie "a=b"\n' in backends
    # The frontend's default HSTS applies only when the response has none.
    assert 'http-response set-header Strict-Transport-Security "max-age=1"\n' in backends


@pytest.mark.parametrize("interval", ["25d", "597h", "35792m", "999999d", "999999h"])
def test_interval_haproxy_would_reject_is_refused(interval: str) -> None:
    route = make_route(health_check={"path": "/", "interval": interval})
    with pytest.raises(RouteError) as error:
        render(make_config(), [route])
    assert ": health_check.interval: " in str(error.value)


@pytest.mark.parametrize("interval", ["1ms", "999999ms", "999999s", "35791m", "596h", "24d"])
def test_interval_up_to_the_limit_is_accepted(interval: str) -> None:
    route = make_route(health_check={"path": "/", "interval": interval})
    backends = render(make_config(), [route]).files["conf.d/20-backends.cfg"]
    assert f"check inter {interval}\n" in backends


def test_route_without_a_health_check_gets_a_tcp_check() -> None:
    backends = render(make_config(), [make_route()]).files["conf.d/20-backends.cfg"]
    assert "option httpchk" not in backends
    assert "check inter 10s\n" in backends


def test_websocket_sets_a_tunnel_timeout_only_when_on() -> None:
    config = make_config()
    assert "timeout tunnel" not in render(config, [make_route()]).files["conf.d/20-backends.cfg"]
    on = render(config, [make_route(websocket=True)]).files["conf.d/20-backends.cfg"]
    assert "timeout tunnel 1h\n" in on


def test_https_port_appears_in_the_redirect_only_when_not_the_default() -> None:
    default = render(make_config(), []).files["conf.d/10-haproxy.cfg"]
    assert "location https://%[req.hdr(host),host_only]%[pathq] code 301" in default
    custom = render(make_config(https_port=8444), []).files["conf.d/10-haproxy.cfg"]
    assert "location https://%[req.hdr(host),host_only]:8444%[pathq] code 301" in custom


def test_disabled_route_is_left_out_and_its_host_stays_known() -> None:
    """Its host answers 404 as a known host, so the CrowdSec scenario does not count its visitors.
    A host that an enabled route also serves keeps that route."""
    off = make_route(enabled=False, hosts=["app.example.com", "both.example.com"])
    release = render(make_config(), [off, make_route("both")])
    assert release.files["hosts.map"] == (
        "app.example.com be_not_found\nboth.example.com route-both\n"
    )
    assert "route-app" not in release.files["conf.d/20-backends.cfg"]


@pytest.mark.parametrize(
    "update",
    [
        {"name": "a\n    server evil 192.168.1.66:80"},
        {"name": 'a"'},
        {"hosts": ["a.example.com\nglobal"]},
        {"hosts": ["a.example.com other"]},
        {"upstreams": [Upstream.model_construct(address="192.168.1.20:80\n  server evil x")]},
        {"upstreams": [Upstream.model_construct(address="192.168.1.20:80 backup")]},
        {"request_headers": {'X-A" if true #': "a"}},
        {"request_headers": {"X-A": "a\n    http-request del-header X"}},
        {"response_headers": {'X-A" if true #': "a"}},
        {"response_headers": {"X-A": "a\n    http-response del-header X"}},
        {"response_headers": {"X-A": "a\rb"}},
    ],
    ids=str,
)
def test_value_that_skipped_its_model_never_reaches_the_output(update: dict[str, Any]) -> None:
    """`model_copy` does not validate, so the filters must hold on their own."""
    route = make_route().model_copy(update=update)
    with pytest.raises(GeneratorError) as error:
        render(make_config(), [route])
    assert "\n" not in str(error.value)
    assert "evil" not in str(error.value)


def test_health_path_that_skipped_its_model_never_reaches_the_output() -> None:
    route = make_route(health_check={"path": "/"})
    assert route.health_check is not None
    check = route.health_check.model_copy(update={"path": "/\n    server evil x"})
    with pytest.raises(GeneratorError):
        render(make_config(), [route.model_copy(update={"health_check": check})])


def test_lan_range_that_skipped_its_model_never_reaches_the_output() -> None:
    config = make_config()
    network = config.network.model_copy(update={"lan_cidrs": ["0.0.0.0/0\nanything"]})
    with pytest.raises(GeneratorError):
        render(config.model_copy(update={"network": network}), [])


def test_templates_write_every_value_through_a_filter() -> None:
    template = ENVIRONMENT.from_string("{{ value }}")
    with pytest.raises(TypeError):
        template.render(value="raw")


def test_write_release_creates_every_file(tmp_path: Path) -> None:
    release = render(make_config(), [make_route()])
    target = tmp_path / "release"
    write_release(release, target)
    written = {
        path.relative_to(target).as_posix(): path.read_text()
        for path in target.rglob("*")
        if path.is_file()
    }
    assert written == dict(release.files)
    assert [path.name for path in tmp_path.iterdir()] == ["release"]
    assert stat.S_IMODE(target.stat().st_mode) == 0o755
    assert stat.S_IMODE((target / "hosts.map").stat().st_mode) == 0o644


def test_write_release_never_changes_an_existing_release(tmp_path: Path) -> None:
    target = tmp_path / "release"
    target.mkdir()
    (target / "hosts.map").write_text("old\n")
    with pytest.raises(FileExistsError):
        write_release(render(make_config(), []), target)
    assert (target / "hosts.map").read_text() == "old\n"
    assert [path.name for path in tmp_path.iterdir()] == ["release"]


def test_failed_write_leaves_nothing_behind(tmp_path: Path) -> None:
    release = render(make_config(), [])
    broken = type(release)(files={**release.files, "hosts.map": "\udc80"})
    with pytest.raises(UnicodeEncodeError):
        write_release(broken, tmp_path / "release")
    assert list(tmp_path.iterdir()) == []
