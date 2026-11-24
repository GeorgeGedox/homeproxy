"""Cross-file rules: each one stops an apply with a message that names the problem."""

import json
import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from homeproxy.auth.server import default_route_address
from homeproxy.geoip.data import GeoipData
from homeproxy.haproxy.apply_rules import (
    check_rules,
    country_warnings,
    fallback_warnings,
    host_group,
    path_only_hosts,
    prometheus_warnings,
    public_ranges,
)
from homeproxy.models import Route, parse_route
from homeproxy.store import Store, load_tree, write_tree
from homeproxy.store.pending import Overlay, PendingChanges

DATA = Path(__file__).parents[4] / "examples" / "data"


@pytest.fixture
def store(tmp_path: Path) -> Store:
    shutil.copytree(DATA, tmp_path / "data")
    return Store(tmp_path / "data")


def overlay_of(store: Store) -> Overlay:
    with store.lock() as held:
        return PendingChanges(store).load_overlay(held)


def errors_of(store: Store) -> list[str]:
    return check_rules(overlay_of(store), store.load_secrets)


def set_config(*keys: str | int, value: object) -> Callable[[Store], None]:
    def edit(store: Store) -> None:
        tree = load_tree(store.config_path)
        node: Any = tree
        for key in keys[:-1]:
            node = node[key]
        node[keys[-1]] = value
        write_tree(store.config_path, tree)

    return edit


def set_route(name: str, key: str, value: object) -> Callable[[Store], None]:
    def edit(store: Store) -> None:
        tree = load_tree(store.route_path(name))
        tree[key] = value
        write_tree(store.route_path(name), tree)

    return edit


def add_route(
    file: str, name: str, host: str, upstream: str = "192.168.1.50:80"
) -> Callable[[Store], None]:
    def edit(store: Store) -> None:
        (store.routes_dir / f"{file}.yaml").write_text(
            f"version: 1\nname: {name}\ntype: http\nhosts: [{host}]\nexposure: lan\n"
            f"upstreams:\n  - address: {upstream}\n"
        )

    return edit


def add_jellyfin_path(file: str, **changes: object) -> Callable[[Store], None]:
    """Add a route on jellyfin's host with jellyfin's host settings and `changes` on top. A change
    to None removes the key."""

    def edit(store: Store) -> None:
        route = {
            "version": 1,
            "name": file,
            "type": "http",
            "hosts": ["jellyfin.example.com"],
            "exposure": "public",
            "upstreams": [{"address": "192.168.1.50:80"}],
            "path_prefix": "/api",
            "cloudflare": {"dns": True, "proxied": True},
            **changes,
        }
        route = {key: value for key, value in route.items() if value is not None}
        # JSON is YAML.
        (store.routes_dir / f"{file}.yaml").write_text(json.dumps(route))

    return edit


def both(
    first: Callable[[Store], None], second: Callable[[Store], None]
) -> Callable[[Store], None]:
    def edit(store: Store) -> None:
        first(store)
        second(store)

    return edit


# example.net loses its certificate. `git` and `whoami` have Cloudflare DNS on example.net hosts.
PLAIN_NET = set_config("domains", 1, value={"zone": "example.net"})
WHOAMI_ON_COM = set_route("whoami", "hosts", ["whoami.example.com"])


def without_cloudflare_dns(store: Store) -> None:
    """Turn off Cloudflare DNS on every seeded route that has it."""
    for name in ("git", "jellyfin", "whoami"):
        set_route(name, "cloudflare", {"dns": False})(store)


def test_the_seeded_config_breaks_no_rule(store: Store) -> None:
    assert errors_of(store) == []


@pytest.mark.parametrize(
    ("edit", "expected"),
    [
        pytest.param(
            add_route("other", "other", "jellyfin.example.com"),
            "jellyfin.example.com is already served by another route",
            id="duplicate-host",
        ),
        pytest.param(add_jellyfin_path("other"), None, id="path-route-shares-a-host"),
        pytest.param(
            both(add_jellyfin_path("api"), add_jellyfin_path("other")),
            "jellyfin.example.com/api is already served by another route",
            id="path-route-on-the-same-path",
        ),
        pytest.param(
            add_jellyfin_path("other", exposure="lan", cloudflare={"dns": True}),
            "jellyfin.example.com is shared with another route, which has a different exposure "
            "and Cloudflare DNS setting",
            id="path-route-differs-in-exposure-and-cloudflare",
        ),
        pytest.param(
            add_jellyfin_path("dns", dns={"target": "192.168.1.20"}),
            "jellyfin.example.com is shared with jellyfin.example.com/api, which has a different "
            "internal DNS answer",
            id="path-route-differs-in-dns-target",
        ),
        pytest.param(
            add_jellyfin_path(
                "other",
                type="redirect",
                redirect={"to": "https://example.net"},
                upstreams=None,
                path_prefix=None,
            ),
            "jellyfin.example.com is already served by another route",
            id="redirect-shares-a-host",
        ),
        pytest.param(
            add_route("other", "other", "app.example.org"),
            "app.example.org is in no configured domain",
            id="host-outside-domains",
        ),
        pytest.param(
            add_route("other", "blog", "blog.example.com"),
            "routes/other.yaml: the route name must be 'other'",
            id="name-differs-from-file",
        ),
        pytest.param(
            set_config("domains", 0, "cloudflare", "ddns_record", value="home.example.net"),
            "domain example.com: ddns_record must be inside the zone",
            id="ddns-record-outside-zone",
        ),
        pytest.param(
            set_config("dns_records", 0, "name", value="nas.example.org"),
            "dns record nas.example.org is in no configured domain",
            id="dns-record-outside-domains",
        ),
        pytest.param(
            set_route("minecraft", "listen_port", 443),
            "port 443 is already used by https_port",
            id="tcp-on-https-port",
        ),
        pytest.param(
            set_route("ssh", "listen_port", 25565),
            "port 25565 is already used by a tcp route",
            id="tcp-on-tcp-port",
        ),
        pytest.param(
            set_config("dashboard", "listen", value="192.168.1.10:443"),
            "dashboard.listen port is already used by https_port",
            id="dashboard-on-https-port",
        ),
        pytest.param(
            set_config("dns_server", "listen", value="192.168.1.10:80"),
            "dns_server.listen port is already used by http_port",
            id="dns-on-http-port",
        ),
        pytest.param(
            set_config("dns_server", "listen", value="192.168.1.10:8443"),
            "dns_server.listen 192.168.1.10:8443 and the dashboard's address 192.168.1.10:8443 "
            "share port 8443; give one of them another port",
            id="dns-on-dashboard-address",
        ),
        pytest.param(
            set_config("dns_server", "listen", value="0.0.0.0:8443"),
            "dns_server.listen 0.0.0.0:8443 and the dashboard's address 192.168.1.10:8443 "
            "share port 8443, and 0.0.0.0 listens on every address; give one of them another port",
            id="dns-on-every-address-at-the-dashboard-port",
        ),
        pytest.param(
            set_config("prometheus", value={"enabled": True, "listen": "192.168.1.10:25565"}),
            "prometheus.listen port is already used by a tcp route",
            id="prometheus-on-tcp-port",
        ),
        pytest.param(
            set_config("prometheus", value={"enabled": True, "listen": "0.0.0.0:5335"}),
            "prometheus.listen 0.0.0.0:5335 and the dns_server's address 192.168.1.10:5335 "
            "share port 5335, and 0.0.0.0 listens on every address; give one of them another port",
            id="prometheus-on-every-address-at-the-dns-port",
        ),
        pytest.param(
            set_config("prometheus", value={"enabled": False, "listen": "192.168.1.10:443"}),
            None,
            id="prometheus-off-binds-nothing",
        ),
        pytest.param(
            set_config("domains", 0, "credentials", value="cf-gone"),
            "domain example.com: credentials 'cf-gone' are not in secrets.yaml",
            id="missing-credentials",
        ),
        pytest.param(
            both(without_cloudflare_dns, set_config("domains", 0, "dns_provider", value="route53")),
            "domain example.com: credentials 'cf-main' are for cloudflare, not route53",
            id="credentials-of-another-provider",
        ),
        pytest.param(
            both(both(PLAIN_NET, WHOAMI_ON_COM), without_cloudflare_dns),
            None,
            id="domain-without-certificate-needs-no-credentials",
        ),
        pytest.param(
            both(PLAIN_NET, without_cloudflare_dns),
            "whoami.example.com, whoami.example.net: hosts mix domains with and without a "
            "certificate",
            id="route-mixes-domains-with-and-without-certificate",
        ),
        pytest.param(
            both(PLAIN_NET, WHOAMI_ON_COM),
            "git.example.net is not in a Cloudflare domain; turn off its Cloudflare DNS switch",
            id="cloudflare-dns-outside-a-cloudflare-domain",
        ),
        pytest.param(
            set_config("crowdsec", "captcha_provider", value="turnstile"),
            None,
            id="captcha-while-crowdsec-is-off",
        ),
        pytest.param(
            set_config("geoip", "provider", value="maxmind"),
            "geoip: the MaxMind license key is not in secrets.yaml",
            id="maxmind-without-key",
        ),
        pytest.param(
            add_route("other", "other", "other.example.com", upstream="localhost:8443"),
            "other.example.com: upstream localhost:8443 is the dashboard",
            id="dashboard-upstream-by-name",
        ),
        pytest.param(
            add_route("other", "other", "other.example.com", upstream="192.168.1.10:8443"),
            "other.example.com: upstream 192.168.1.10:8443 is the dashboard",
            id="dashboard-upstream-by-lan-ip",
        ),
    ],
)
def test_a_broken_rule_is_reported(
    store: Store, edit: Callable[[Store], None], expected: str | None
) -> None:
    edit(store)

    assert errors_of(store) == ([expected] if expected else [])


def test_a_dashboard_on_every_address_is_refused_at_any_address_of_this_host(store: Store) -> None:
    """On 0.0.0.0 the dashboard also answers on a second LAN or VPN address of the host."""
    own = default_route_address()
    if own is None or own == "192.168.1.10":
        pytest.skip("this host needs an address other than haproxy_lan_ip")
    set_config("dashboard", "listen", value="0.0.0.0:8443")(store)
    add_route("other", "other", "other.example.com", upstream=f"{own}:8443")(store)

    assert errors_of(store) == [f"other.example.com: upstream {own}:8443 is the dashboard"]


@pytest.mark.parametrize(
    ("provider", "bouncer_key", "expected"),
    [
        ("turnstile", "fake-bouncer-key", "crowdsec: the captcha keys are not in secrets.yaml"),
        ("none", "", "crowdsec: the bouncer key is not in secrets.yaml"),
    ],
    ids=["captcha-keys", "bouncer-key"],
)
def test_crowdsec_needs_its_keys_while_it_is_on(
    store: Store, provider: str, bouncer_key: str, expected: str
) -> None:
    set_config("crowdsec", "enabled", value=True)(store)
    set_config("crowdsec", "captcha_provider", value=provider)(store)
    secrets_path = store.secrets_dir / "secrets.yaml"
    secrets = load_tree(secrets_path)
    secrets["crowdsec"]["bouncer_key"] = bouncer_key
    write_tree(secrets_path, secrets)

    assert errors_of(store) == [expected]


@pytest.mark.parametrize(
    ("field", "cidr", "public"),
    [
        ("network.lan_cidrs", "0.0.0.0/1", True),
        ("network.lan_cidrs", "128.0.0.0/1", True),
        ("network.lan_cidrs", "10.0.0.0/7", True),
        ("network.lan_cidrs", "2001:db8::/32", True),
        ("network.lan_cidrs", "8.8.8.8", True),
        ("network.lan_cidrs", "192.168.1.0/24", False),
        ("network.lan_cidrs", "127.0.0.0/8", False),
        ("network.lan_cidrs", "fd00::/8", False),
        ("crowdsec.allowlist", "198.51.0.0/16", True),
        ("crowdsec.allowlist", "8.8.8.8", False),
    ],
    ids=[
        "lower-half",
        "upper-half",
        "straddles-10/8",
        "ipv6-public",
        "lan-single-address",
        "rfc1918",
        "loopback",
        "ula",
        "allowlist-range",
        "allowlist-single-address",
    ],
)
def test_a_public_range_is_a_warning(field: str, cidr: str, public: bool) -> None:
    """A range reaching outside the private ranges warns, naming it. One allowlisted address
    does not."""
    effect = {
        "network.lan_cidrs": "its clients count as LAN",
        "crowdsec.allowlist": "CrowdSec never checks its clients",
    }[field]
    kind = "range" if "/" in cidr else "address"
    expected = [f"{field}: {cidr} is a public {kind}, so {effect}"]

    assert public_ranges(field, [cidr]) == (expected if public else [])


@pytest.mark.parametrize(
    ("countries", "git_on", "expected"),
    [
        (None, True, ["countries are blocked, but there is no GeoIP data: see Security → GeoIP"]),
        (["CN"], True, ["not in the GeoIP data, so not blocked: AQ"]),
        (["CN"], False, []),
        (["AQ", "CN"], True, []),
    ],
    ids=["no-data", "code-missing", "route-list-off", "all-in-data"],
)
def test_a_blocked_country_the_data_lacks_is_a_warning(
    store: Store, countries: list[str] | None, git_on: bool, expected: list[str]
) -> None:
    """The global list blocks CN and the example git route blocks AQ, unless its list is off."""
    set_config("geoip", "block_countries", value=["CN"])(store)
    set_route("git", "geoip", {"enabled": git_on, "block_countries": ["AQ"]})(store)
    overlay = overlay_of(store)
    data = None
    if countries is not None:
        data = GeoipData(
            provider="dbip-lite", updated_at=datetime.now(UTC), entries=1, countries=countries
        )

    assert country_warnings(overlay.config, overlay.routes, data) == expected


def test_no_block_list_needs_no_geoip_data(store: Store) -> None:
    assert country_warnings(overlay_of(store).config, [], None) == []


@pytest.mark.parametrize(
    ("enabled", "listen", "warns"),
    [
        (True, "192.0.2.1:8405", True),
        (True, "127.0.0.1:8405", False),
        (False, "192.0.2.1:8405", False),
    ],
    ids=["address-missing", "address-present", "disabled"],
)
def test_an_exporter_address_this_host_lacks_is_a_warning(
    store: Store, enabled: bool, listen: str, warns: bool
) -> None:
    """HAProxy binds the exporter even without its address, so a typo would go unnoticed."""
    set_config("prometheus", value={"enabled": enabled, "listen": listen})(store)
    expected = [
        "prometheus.listen: this host has no address 192.0.2.1 yet, so the exporter answers "
        "only once it has"
    ]

    assert prometheus_warnings(overlay_of(store).config) == (expected if warns else [])


@pytest.mark.parametrize(
    ("dashboard", "fallback", "warns"),
    [
        ("192.168.1.10:9443", "192.168.1.10:8443", True),
        ("192.168.1.10:8443", "192.168.1.10:8443", False),
        ("192.168.1.10:9443", "not an address", False),
    ],
    ids=["other-port", "own-port-is-refused-instead", "no-fallback-possible"],
)
def test_a_public_upstream_on_the_dashboard_fallback_address_is_a_warning(
    store: Store, monkeypatch: pytest.MonkeyPatch, dashboard: str, fallback: str, warns: bool
) -> None:
    """The dashboard falls back there for private sources, HAProxy included. A bad variable must
    not fail the apply: the dashboard then cannot fall back at all."""
    monkeypatch.setenv("HOMEPROXY_BOOTSTRAP_LISTEN", fallback)
    set_config("dashboard", "listen", value=dashboard)(store)
    add_route("unifi", "unifi", "unifi.example.com", upstream="192.168.1.10:8443")(store)
    set_route("unifi", "exposure", "public")(store)
    overlay = overlay_of(store)
    expected = [
        "unifi.example.com: upstream 192.168.1.10:8443 is where the dashboard listens when "
        "dashboard.listen cannot be bound, and then this route would make its login public"
    ]

    assert fallback_warnings(overlay.config, overlay.routes) == (expected if warns else [])


ON_MEDIA_ONLY = set_route("jellyfin", "path_prefix", "/media")


@pytest.mark.parametrize(
    ("edit", "paths"),
    [
        (ON_MEDIA_ONLY, "/media"),
        (add_jellyfin_path("api"), None),
        (both(add_jellyfin_path("api"), set_route("jellyfin", "enabled", False)), "/api"),
        (both(ON_MEDIA_ONLY, set_route("jellyfin", "dns", {"target": "192.168.1.10"})), None),
    ],
    ids=["path-only", "with-root-route", "root-route-disabled", "dns-target-elsewhere"],
)
def test_a_host_routed_only_on_paths_is_a_warning(
    store: Store, edit: Callable[[Store], None], paths: str | None
) -> None:
    """The internal DNS server sends the LAN to HAProxy for the whole host, so every path without a
    route gets 404 there. A `/` route, or a DNS target past HAProxy, gives no warning."""
    edit(store)
    expected = {
        "jellyfin.example.com": f"jellyfin.example.com is routed only on {paths}, so on the LAN "
        "its other paths get 404. Add a route for /, or move these paths to their own host."
    }

    assert path_only_hosts(overlay_of(store).routes) == (expected if paths else {})


def site_route(name: str, kind: str, *hosts: str, path: str = "/") -> Route:
    """A route of type `kind` on `hosts`, at `path` when it is `http`."""
    route: dict[str, Any] = {
        "version": 1,
        "name": name,
        "type": kind,
        "hosts": list(hosts),
        "exposure": "lan",
        "upstreams": [{"address": "192.168.1.50:80"}],
    }
    if kind == "http":
        route["path_prefix"] = path
    return parse_route(route)


SITES = [
    site_route("api", "http", "app.example.com", path="/api"),
    site_route("app", "http", "app.example.com"),
    site_route("docs", "http", "docs.example.com", path="/docs"),
    site_route("nas", "passthrough", "nas.example.com", "files.example.com"),
    site_route("wiki", "http", "wiki.example.com", "docs.example.com"),
]


@pytest.mark.parametrize(
    ("host", "names"),
    [
        ("app.example.com", ["api", "app"]),
        ("wiki.example.com", ["docs", "wiki"]),
        ("files.example.com", ["nas"]),
        ("other.example.com", []),
    ],
    ids=["one-host", "joined-through-a-shared-host", "not-http-alone", "no-route"],
)
def test_a_host_group_holds_the_routes_that_share_its_settings(host: str, names: list[str]) -> None:
    """The routes on the host and the `http` routes joined to them through shared hosts must agree
    on exposure, Cloudflare and DNS. Only `http` routes share a host."""
    assert [route.name for route in host_group(SITES, host)] == names
