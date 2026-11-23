"""Golden files: route YAML in, expected HAProxy files out, then `haproxy -c` on each case.

Each directory here with an `input/` is one case. `mise run golden-update` rewrites `expected/`.
"""

import json
import os
import re
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError
from ruamel.yaml import YAML

from homeproxy.generator import GeneratorError, RenderedRelease, render, write_release
from homeproxy.generator.crowdsec import bouncer_config
from homeproxy.logs import HTTP_LOG_FORMAT, SYSLOG_SOCKET, TCP_LOG_FORMAT
from homeproxy.models import Config, RedirectRoute, Secrets, parse_route
from homeproxy.models.route import Upstream
from homeproxy.models.secrets import SecretsCrowdsec
from homeproxy.util.run import RunResult

GOLDEN = Path(__file__).parent
CASES = sorted(path.parent.name for path in GOLDEN.glob("*/input"))
UPDATE = os.environ.get("HOMEPROXY_GOLDEN_UPDATE") == "1"
EXAMPLES = GOLDEN.parents[2] / "examples" / "data" / "config"

HaproxyCheck = Callable[[Path, str], RunResult]


def load_yaml(path: Path) -> Any:
    """Load with the round-trip loader the store uses, because its types differ from plain YAML."""
    return YAML(typ="rt").load(path)


def render_directory(
    config_file: Path,
    secrets_file: Path,
    routes_dir: Path,
    includes_dir: Path | None = None,
    country_map: str = "",
) -> RenderedRelease:
    config = Config.model_validate(load_yaml(config_file))
    routes = [parse_route(load_yaml(path)) for path in routes_dir.glob("*.yaml")]
    includes = {}
    if includes_dir is not None:
        includes = {
            path.stem: path.read_text(encoding="utf-8") for path in includes_dir.glob("*.cfg")
        }
    basic_auth = Secrets.model_validate(load_yaml(secrets_file)).basic_auth
    return render(config, routes, includes, basic_auth, country_map=country_map)


def render_case(case: str) -> RenderedRelease:
    own_config = GOLDEN / case / "input" / "config.yaml"
    config_file = own_config if own_config.exists() else GOLDEN / "base-config.yaml"
    own_secrets = GOLDEN / case / "input" / "basic-auth.yaml"
    secrets_file = own_secrets if own_secrets.exists() else GOLDEN / "base-basic-auth.yaml"
    case_input = GOLDEN / case / "input"
    country_map = case_input / "country.map"
    return render_directory(
        config_file,
        secrets_file,
        case_input / "routes",
        case_input / "includes",
        country_map.read_text() if country_map.exists() else "",
    )


def read_expected(expected_dir: Path) -> dict[str, str]:
    return {
        path.relative_to(expected_dir).as_posix(): path.read_text(encoding="utf-8")
        for path in expected_dir.rglob("*")
        if path.is_file()
    }


# HAProxy times one `crypt` of each user's hash by the clock and warns from 10 ms. A SHA-512
# crypt hash with 5000 rounds takes about 1 ms, so the warning shows only a descheduled check.
SLOW_HASH_WARNING = re.compile(r"^.*the hash algorithm used for this password takes .*$\n?", re.M)


def assert_accepted(result: RunResult) -> None:
    output = SLOW_HASH_WARNING.sub("", result.stdout + result.stderr)
    assert result.returncode == 0, result.stderr
    assert "[WARNING]" not in output, output
    assert "[ALERT]" not in output, output


def test_every_required_case_exists() -> None:
    assert {
        "attack-header-values",
        "basic-auth",
        "cloudflare-proxied",
        "crowdsec-logging",
        "geoip-blocking",
        "attack-health-paths",
        "custom-ports",
        "includes",
        "lan-only-route",
        "no-routes",
        "path-routes",
        "plain-http-domain",
        "public-route",
        "redirect-routes",
        "reserved-names",
        "route-types",
        "upstream-tls",
    } <= set(CASES)


@pytest.mark.parametrize("case", CASES)
def test_rendered_files_match_golden_files(case: str) -> None:
    files = render_case(case).files
    expected_dir = GOLDEN / case / "expected"
    if UPDATE:
        shutil.rmtree(expected_dir, ignore_errors=True)
        for relative_path, content in files.items():
            path = expected_dir / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
    assert read_expected(expected_dir) == dict(files)


@pytest.mark.parametrize("case", CASES)
def test_haproxy_accepts_golden_case(
    case: str, data_dir: Path, haproxy_check: HaproxyCheck
) -> None:
    write_release(render_case(case), data_dir / "releases" / "new")
    assert_accepted(haproxy_check(data_dir, "releases/new"))


def test_haproxy_accepts_the_seeded_examples(data_dir: Path, haproxy_check: HaproxyCheck) -> None:
    secrets_file = EXAMPLES.parent / "secrets" / "secrets.yaml"
    release = render_directory(EXAMPLES / "config.yaml", secrets_file, EXAMPLES / "routes")
    write_release(release, data_dir / "releases" / "new")
    assert_accepted(haproxy_check(data_dir, "releases/new"))


def test_header_attacks_stay_literal_in_the_output() -> None:
    backends = render_case("attack-header-values").files["conf.d/20-backends.cfg"]
    # `%%` is a literal percent and `\$` a literal dollar, so neither may appear alone.
    assert re.search(r"(?<!%)(%%)*%(?!%)", backends) is None
    assert re.search(r"(?<!\\)(\\\\)*\$", backends) is None
    rules = re.findall(r"^    http-re(?:quest|sponse) set-header (\S+) (.*)$", backends, re.M)
    assert len(rules) == 2 * 17
    for _, value in rules:
        # Each value is one double-quoted word: no quote can end it before the end of the line.
        assert re.fullmatch(r'"([^"\\]|\\.)*"', value) is not None


def test_health_check_attacks_stay_one_word() -> None:
    backends = render_case("attack-health-paths").files["conf.d/20-backends.cfg"]
    rules = re.findall(
        r"^    http-check send meth GET uri (\S+) ver HTTP/1\.1 ", backends, re.MULTILINE
    )
    assert sorted(rules) == ['"/%00"', '"/%0a"', '"/%25?a=%5B&b=%&c=+"']


@pytest.mark.parametrize(
    ("broken_file", "content"),
    [
        ("hosts.map", None),
        ("lan-only.map", "app.example.com not-a-number\n"),
        ("hsts.map", "app.example.com not-a-number\n"),
        ("lan.lst", "not-an-address\n"),
    ],
    ids=["hosts-map-missing", "lan-only-map-bad-value", "hsts-map-bad-value", "lan-list-bad-entry"],
)
def test_broken_map_fails_the_check_while_the_live_release_stays_fine(
    broken_file: str, content: str | None, data_dir: Path, haproxy_check: HaproxyCheck
) -> None:
    release = render_case("public-route")
    write_release(release, data_dir / "releases" / "live")
    (data_dir / "generated").symlink_to("releases/live")
    write_release(release, data_dir / "releases" / "new")
    broken = data_dir / "releases" / "new" / broken_file
    if content is None:
        broken.unlink()
    else:
        broken.write_text(content)

    failed = haproxy_check(data_dir, "releases/new")
    assert failed.returncode != 0
    assert broken_file in failed.stdout + failed.stderr

    assert_accepted(haproxy_check(data_dir, "generated"))
    assert (data_dir / "generated").readlink() == Path("releases/live")
    assert read_expected(data_dir / "releases" / "live") == dict(release.files)


def test_check_reads_the_maps_of_the_release_it_is_given(
    data_dir: Path, haproxy_check: HaproxyCheck
) -> None:
    """A good new release passes beside a broken live one, so its paths are truly relative."""
    release = render_case("public-route")
    write_release(release, data_dir / "releases" / "live")
    (data_dir / "generated").symlink_to("releases/live")
    (data_dir / "releases" / "live" / "lan.lst").write_text("not-an-address\n")
    write_release(release, data_dir / "releases" / "new")

    assert_accepted(haproxy_check(data_dir, "releases/new"))
    assert haproxy_check(data_dir, "generated").returncode != 0


def test_leftover_temp_file_in_the_certs_directory_does_not_break_the_check(
    data_dir: Path, haproxy_check: HaproxyCheck
) -> None:
    """A crash during an atomic write leaves a dot file, and HAProxy must ignore it."""
    write_release(render_case("public-route"), data_dir / "releases" / "new")
    (data_dir / "certs" / ".default.pem.partial").write_text("partial")
    assert_accepted(haproxy_check(data_dir, "releases/new"))


APPENDED_FIELDS = "%{+Q}[var(txn.route)] %{+Q}[var(txn.country)]"


def log_targets(case: str) -> list[str]:
    main_config = render_case(case).files["conf.d/10-haproxy.cfg"]
    return re.findall(r"^    log (\S+) ", main_config, re.MULTILINE)


def test_log_formats_are_the_ones_the_parser_reads() -> None:
    main_config = render_case("public-route").files["conf.d/10-haproxy.cfg"]
    formats = re.findall(r'^    log-format "(.*)"$', main_config, re.MULTILINE)
    assert formats == [
        f"{TCP_LOG_FORMAT} {APPENDED_FIELDS} %{{+Q}}[var(txn.sni),url_enc]",
        f"{HTTP_LOG_FORMAT} {APPENDED_FIELDS} %{{+Q}}[ssl_fc_sni,url_enc]",
    ]


def test_access_lines_go_to_crowdsec_only_while_it_is_enabled() -> None:
    assert log_targets("public-route") == [SYSLOG_SOCKET, "stdout"]
    assert log_targets("crowdsec-logging") == [SYSLOG_SOCKET, "127.0.0.1:15140", "stdout"]


LISTEN_ATTACKS = [
    "127.0.0.1:15140\n    log stdout format raw local0",
    "127.0.0.1:15140 format raw",
    "127.0.0.1:15140#",
    '"127.0.0.1:15140"',
    "${HOME}:15140",
    "-127.0.0.1:15140",
]
# The case that enables each section, and the listen field in it.
LISTEN_FIELDS = [
    ("crowdsec-logging", "crowdsec", "syslog"),
    ("custom-ports", "prometheus", "listen"),
]


@pytest.mark.parametrize("value", LISTEN_ATTACKS)
@pytest.mark.parametrize(("case", "section", "field"), LISTEN_FIELDS)
def test_listen_address_attack_values_never_reach_the_output(
    case: str, section: str, field: str, value: str
) -> None:
    config = load_yaml(GOLDEN / case / "input" / "config.yaml")
    config[section][field] = value
    with pytest.raises(ValidationError):
        Config.model_validate(config)

    # `model_copy` does not validate, so the filter must hold on its own.
    valid = Config.model_validate(load_yaml(GOLDEN / case / "input" / "config.yaml"))
    changed = getattr(valid, section).model_copy(update={field: value})
    with pytest.raises(GeneratorError) as error:
        render(valid.model_copy(update={section: changed}), [])
    assert value not in str(error.value)


@pytest.mark.parametrize(
    "value", ["173.245.48.0/20\n    http-request allow", "173.245.48.0/20 #", "${HOME}"]
)
def test_cloudflare_range_attack_values_never_reach_the_output(value: str) -> None:
    """The refresh job validates each range. The filter must hold even for a value it did not."""
    config = Config.model_validate(load_yaml(GOLDEN / "base-config.yaml"))
    with pytest.raises(GeneratorError) as error:
        render(config, [], cloudflare_ranges=[value])
    assert value not in str(error.value)


@pytest.mark.parametrize("value", ["203.0.113.7\n    http-request allow", "203.0.113.7 #"])
def test_allowlist_attack_values_never_reach_the_output(value: str) -> None:
    config = Config.model_validate(load_yaml(GOLDEN / "crowdsec-logging" / "input" / "config.yaml"))
    crowdsec = config.crowdsec.model_copy(update={"allowlist": [value]})
    with pytest.raises(GeneratorError) as error:
        render(config.model_copy(update={"crowdsec": crowdsec}), [])
    assert value not in str(error.value)


@pytest.mark.parametrize("value", ["CN 1\nroute-other/US", "CN\n    http-request allow"])
def test_country_code_attack_values_never_reach_the_output(value: str) -> None:
    """`model_copy` skips the country code pattern, so the filter must hold on its own."""
    case = GOLDEN / "geoip-blocking" / "input"
    config = Config.model_validate(load_yaml(case / "config.yaml"))
    route = parse_route(load_yaml(case / "routes" / "plain.yaml"))
    geoip = config.geoip.model_copy(update={"block_countries": [value]})
    with pytest.raises(GeneratorError) as error:
        render(config.model_copy(update={"geoip": geoip}), [route])
    assert value not in str(error.value)


def test_a_route_block_list_that_is_off_renders_as_an_empty_list() -> None:
    """Neither the route's rules nor the map carry its countries. US is only on `app`'s list."""
    case = GOLDEN / "geoip-blocking" / "input"
    config = Config.model_validate(load_yaml(case / "config.yaml"))
    country_map = (case / "country.map").read_text()
    data = load_yaml(case / "routes" / "app.yaml")
    off = parse_route(data | {"geoip": {"enabled": False, "block_countries": ["RU", "US"]}})
    empty = parse_route(data | {"geoip": {}})

    assert render(config, [off], country_map=country_map) == render(
        config, [empty], country_map=country_map
    )


@pytest.mark.parametrize("line", ["1.2.3/24 DE", "::1::/64 DE", "1.2.3.4/99 DE"])
def test_a_country_map_line_that_is_no_network_stops_the_render(line: str) -> None:
    """Such a line passes the saved map's character check. The render refuses it, so an apply
    fails at validate instead of with an unexpected error."""
    case = GOLDEN / "geoip-blocking" / "input"
    config = Config.model_validate(load_yaml(case / "config.yaml"))
    route = parse_route(load_yaml(case / "routes" / "plain.yaml"))
    with pytest.raises(GeneratorError, match=r"country\.map is not valid"):
        render(config, [route], country_map=f"{line}\n")


@pytest.mark.parametrize(
    ("route_file", "update"),
    [
        ("passthrough", {"hosts": ["git.example.com\n    tcp-request content accept"]}),
        (
            "passthrough",
            {"upstreams": [Upstream.model_construct(address="192.168.1.23:443 backup")]},
        ),
        ("tcp", {"listen_port": "25565\n    bind *:22"}),
    ],
    ids=["sni-host", "stream-server", "listen-port"],
)
def test_route_type_attack_values_never_reach_the_output(
    route_file: str, update: dict[str, Any]
) -> None:
    """`model_copy` skips the route model, so the filters must hold on their own."""
    config = Config.model_validate(load_yaml(GOLDEN / "base-config.yaml"))
    route_path = GOLDEN / "route-types" / "input" / "routes" / f"{route_file}.yaml"
    route = parse_route(load_yaml(route_path)).model_copy(update=update)
    with pytest.raises(GeneratorError) as error:
        render(config, [route])
    for value in update.values():
        assert str(value) not in str(error.value)


@pytest.mark.parametrize(
    "value",
    [
        "https://example.com\n    http-request allow",
        "https://example.com/ x",
        "https://example.com/%[req.hdr(cookie)]",
        'https://example.com/"',
        "https://example.com/|",
    ],
    ids=["newline", "space", "sample-fetch", "quote", "field-separator"],
)
def test_redirect_target_attack_values_never_reach_the_output(value: str) -> None:
    """`model_copy` skips the route model, so the filter must hold on its own."""
    config = Config.model_validate(load_yaml(GOLDEN / "base-config.yaml"))
    route = parse_route(load_yaml(GOLDEN / "redirect-routes" / "input" / "routes" / "www.yaml"))
    assert isinstance(route, RedirectRoute)
    redirect = route.redirect.model_copy(update={"to": value})
    with pytest.raises(GeneratorError) as error:
        render(config, [route.model_copy(update={"redirect": redirect})])
    assert value not in str(error.value)


@pytest.mark.parametrize(
    "value",
    ["/api\n    http-request allow", "/a b", ".evil.example.net", "/a/../b", "/a%2fb"],
    ids=["newline", "space", "joins-another-host", "dot-segment", "percent"],
)
def test_path_prefix_attack_values_never_reach_the_output(value: str) -> None:
    """`model_copy` skips the route model, so the filter must hold on its own."""
    config = Config.model_validate(load_yaml(GOLDEN / "base-config.yaml"))
    route = parse_route(load_yaml(GOLDEN / "path-routes" / "input" / "routes" / "api.yaml"))
    with pytest.raises(GeneratorError) as error:
        render(config, [route.model_copy(update={"path_prefix": value})])
    assert value not in str(error.value)


def test_bouncer_config_holds_each_key_as_one_value() -> None:
    """The keys come from `secrets.yaml`. A quote or newline in one must not add a setting, and
    neither may one in `appsec_url`, which the model refuses."""
    config = Config.model_validate(load_yaml(GOLDEN / "crowdsec-logging" / "input" / "config.yaml"))
    attack = 'key"\nlisten_tcp: 0.0.0.0:9000'
    crowdsec = config.crowdsec.model_copy(
        update={"captcha_provider": "turnstile", "appsec": True, "appsec_url": attack}
    )
    # `model_construct` skips validation, as an attacker's value would need to.
    keys = SecretsCrowdsec.model_construct(
        bouncer_key=SecretStr(attack),
        captcha_site_key=SecretStr("site"),
        captcha_secret_key=SecretStr("secret"),
        captcha_signing_key=SecretStr("s" * 32),
    )

    assert json.loads(bouncer_config(crowdsec, keys)) == {
        "log_mode": "stdout",
        "log_level": "info",
        "update_frequency": "10s",
        "api_url": "http://127.0.0.1:18080/",
        "api_key": attack,
        "listen_unix": "/run/crowdsec-spoa/spoa.sock",
        "prometheus": {"enabled": False},
        "appsec_url": f"{attack}/",
        "hosts": [
            {
                "host": "*",
                "captcha": {
                    "provider": "turnstile",
                    "site_key": "site",
                    "secret_key": "secret",
                    "signing_key": "s" * 32,
                    "fallback_remediation": "ban",
                },
            }
        ],
    }
