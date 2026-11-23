"""Validation of `config.yaml` and its domains."""

from typing import Any

import pytest
from pydantic import ValidationError

from homeproxy.models import Config, Domain

PASSWORD_HASH = "$argon2id$v=19$m=65536,t=3,p=4$c2FsdHNhbHQ$aGFzaGhhc2hoYXNo"  # noqa: S105


def config(**changes: Any) -> dict[str, Any]:
    return {
        "version": 1,
        "dashboard": {"listen": "192.168.1.10:8443", "password_hash": PASSWORD_HASH},
        "network": {"lan_cidrs": ["192.168.1.0/24"], "haproxy_lan_ip": "192.168.1.10"},
        "dns_server": {"listen": "192.168.1.10:5335"},
        **changes,
    }


def domain(**changes: Any) -> dict[str, Any]:
    return {
        "zone": "example.com",
        "dns_provider": "cloudflare",
        "credentials": "cf-main",
        **changes,
    }


def network(**changes: Any) -> dict[str, Any]:
    return {"lan_cidrs": ["192.168.1.0/24"], "haproxy_lan_ip": "192.168.1.10", **changes}


def error_locations(data: dict[str, Any]) -> list[tuple[str | int, ...]]:
    with pytest.raises(ValidationError) as info:
        Config.model_validate(data)
    return [error["loc"] for error in info.value.errors()]


def test_minimal_config_gets_defaults() -> None:
    loaded = Config.model_validate(config())
    assert loaded.dashboard.session_ttl == "12h"
    assert loaded.network.public_ip == "auto"
    assert loaded.network.ipv6 is False
    assert loaded.cloudflare.enabled is False
    assert loaded.domains == []
    assert loaded.crowdsec.enabled is False
    assert loaded.crowdsec.lapi_url is None
    assert loaded.crowdsec.appsec is False
    assert loaded.crowdsec.captcha_provider == "none"
    assert loaded.geoip.provider == "dbip-lite"
    assert loaded.haproxy.track_branch == "lts"
    assert loaded.prometheus.enabled is False
    assert loaded.prometheus.listen is None


@pytest.mark.parametrize("missing", ["version", "dashboard", "network", "dns_server"])
def test_required_section_is_reported(missing: str) -> None:
    data = config()
    del data[missing]
    assert error_locations(data) == [(missing,)]


@pytest.mark.parametrize(
    "value",
    [
        "fe80::1%a\nb",
        "fe80::1%eth0",
        "192.168.1.0/24\n",
        '192.168.1.0/24"',
        "192.168.1.1/24",
        "192.168.1.0/33",
        "256.0.0.0/8",
        "192.168.1.0/024",
        "10.0.0.0/08",
        "fd00::/008",
        "lan",
        "",
    ],
    ids=ascii,
)
def test_bad_lan_range_is_rejected(value: str) -> None:
    data = config(network=network(lan_cidrs=[value]))
    assert error_locations(data) == [("network", "lan_cidrs", 0)]


def test_lan_ranges_and_allowlist_accept_ipv4_and_ipv6() -> None:
    entries = ["10.0.0.0/8", "fd00::/8", "192.168.1.5", "2001:db8::1"]
    loaded = Config.model_validate(
        config(network=network(lan_cidrs=entries), crowdsec={"allowlist": entries})
    )
    assert loaded.network.lan_cidrs == entries
    assert loaded.crowdsec.allowlist == entries


CATCH_ALLS = [
    "0.0.0.0/0",
    "::/0",
    "0:0:0:0:0:0:0:0/0",
    "0000::/0",
    "::0/0",
    "::ffff:0:0/96",
    "::ffff:0.0.0.0/96",
    "::/80",
]


@pytest.mark.parametrize("value", CATCH_ALLS)
def test_catch_all_range_is_rejected(value: str) -> None:
    assert error_locations(config(network=network(lan_cidrs=[value]))) == [
        ("network", "lan_cidrs", 0)
    ]
    assert error_locations(config(crowdsec={"allowlist": [value]})) == [
        ("crowdsec", "allowlist", 0)
    ]


@pytest.mark.parametrize("value", ["0.0.0.0/1", "128.0.0.0/1", "198.51.100.0/24", "2000::/3"])
def test_narrower_public_range_is_accepted(value: str) -> None:
    loaded = Config.model_validate(
        config(network=network(lan_cidrs=[value]), crowdsec={"allowlist": [value]})
    )
    assert loaded.network.lan_cidrs == [value]
    assert loaded.crowdsec.allowlist == [value]


def test_empty_lan_ranges_are_rejected() -> None:
    assert error_locations(config(network=network(lan_cidrs=[]))) == [("network", "lan_cidrs")]


@pytest.mark.parametrize(
    "value",
    ["192.168.1.010", "192.168.1", "256.1.1.1", "192.168.1.10\n", "fd00::1", "lan", ""],
    ids=ascii,
)
def test_bad_lan_ip_is_rejected(value: str) -> None:
    data = config(network=network(haproxy_lan_ip=value))
    assert error_locations(data) == [("network", "haproxy_lan_ip")]


@pytest.mark.parametrize(
    "value",
    [
        "192.168.1.10",
        "192.168.1.10:0",
        "192.168.1.10:65536",
        "192.168.1.10:00443",
        "192.168.1.10:8443\n",
        "localhost:8443",
        "[::1]:8443",
        '192.168.1.10:8443"',
    ],
    ids=ascii,
)
def test_bad_listen_address_is_rejected(value: str) -> None:
    data = config(dashboard={"listen": value, "password_hash": PASSWORD_HASH})
    assert error_locations(data) == [("dashboard", "listen")]
    assert error_locations(config(dns_server={"listen": value})) == [("dns_server", "listen")]
    data = config(prometheus={"enabled": True, "listen": value})
    assert error_locations(data) == [("prometheus", "listen")]


@pytest.mark.parametrize(
    "value",
    ["", "secret", "**********", f"{PASSWORD_HASH}\n", f"{PASSWORD_HASH}\nglobal", "$2b$12$abc"],
    ids=ascii,
)
def test_bad_password_hash_is_rejected(value: str) -> None:
    data = config(dashboard={"listen": "192.168.1.10:8443", "password_hash": value})
    assert error_locations(data) == [("dashboard", "password_hash")]


@pytest.mark.parametrize("ttl", ["12", "0h", "h", "12 h", "12h\n", "-1h", 12], ids=ascii)
def test_bad_session_ttl_is_rejected(ttl: object) -> None:
    data = config(
        dashboard={
            "listen": "192.168.1.10:8443",
            "password_hash": PASSWORD_HASH,
            "session_ttl": ttl,
        }
    )
    assert error_locations(data) == [("dashboard", "session_ttl")]


def test_password_hash_is_masked_outside_the_file_dump() -> None:
    loaded = Config.model_validate(config())
    assert PASSWORD_HASH not in repr(loaded)
    assert PASSWORD_HASH not in loaded.model_dump_json()


@pytest.mark.parametrize(
    ("field", "value"), [("http_port", "80"), ("https_port", 0), ("https_port", 65536)]
)
def test_bad_port_is_rejected(field: str, value: object) -> None:
    assert error_locations(config(network=network(**{field: value}))) == [("network", field)]


def test_http_and_https_ports_must_differ() -> None:
    data = config(network=network(http_port=8080, https_port=8080))
    assert error_locations(data) == [("network", "https_port")]


def test_http_port_must_differ_from_the_default_https_port() -> None:
    assert error_locations(config(network=network(http_port=443))) == [("network", "https_port")]


def test_https_port_must_differ_from_the_default_http_port() -> None:
    assert error_locations(config(network=network(https_port=80))) == [("network", "https_port")]


def test_default_ports_are_valid() -> None:
    loaded = Config.model_validate(config())
    assert (loaded.network.http_port, loaded.network.https_port) == (80, 443)


@pytest.mark.parametrize("value", ["none", "1.1.1.1\n", "256.1.1.1", ""], ids=ascii)
def test_bad_public_ip_is_rejected(value: str) -> None:
    assert error_locations(config(network=network(public_ip=value))) == [("network", "public_ip")]


def test_fixed_public_ip_is_accepted() -> None:
    loaded = Config.model_validate(config(network=network(public_ip="192.0.2.10")))
    assert loaded.network.public_ip == "192.0.2.10"


@pytest.mark.parametrize(
    "value", ["example.com\n", 'example.com"', "EXAMPLE.com", "com"], ids=ascii
)
def test_bad_zone_is_rejected(value: str) -> None:
    assert error_locations(config(domains=[domain(zone=value)])) == [("domains", 0, "zone")]


@pytest.mark.parametrize(
    ("changes", "location"),
    [
        ({"dns_provider": "exec"}, ("domains", 0, "dns_provider")),
        ({"dns_provider": "manual"}, ("domains", 0, "dns_provider")),
        ({"dns_provider": "route54"}, ("domains", 0, "dns_provider")),
        ({"credentials": "cf main\n"}, ("domains", 0, "credentials")),
        ({"credentials": 'cf"main'}, ("domains", 0, "credentials")),
        (
            {"cloudflare": {"ddns_record": "home\n.example.com"}},
            ("domains", 0, "cloudflare", "ddns_record"),
        ),
        ({"cloudflare": {"ddns_update": "yes"}}, ("domains", 0, "cloudflare", "ddns_update")),
    ],
    ids=repr,
)
def test_bad_domain_field_is_rejected(
    changes: dict[str, Any], location: tuple[str | int, ...]
) -> None:
    assert error_locations(config(domains=[domain(**changes)])) == [location]


def test_required_domain_fields_are_reported() -> None:
    assert error_locations(config(domains=[{}])) == [("domains", 0, "zone")]


def test_domain_without_provider_and_credentials_has_no_certificate() -> None:
    loaded = Domain.model_validate({"zone": "example.com"})
    assert (loaded.has_certificate, loaded.wildcard_cert) == (False, False)
    assert Domain.model_validate(domain()).has_certificate


def test_self_signed_domain_has_a_certificate_without_credentials() -> None:
    loaded = Domain.model_validate({"zone": "example.com", "dns_provider": "self-signed"})
    assert (loaded.has_certificate, loaded.wildcard_cert) == (True, True)


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        pytest.param({"credentials": None}, "credentials", id="provider-without-credentials"),
        pytest.param({"dns_provider": None}, "dns_provider", id="credentials-without-provider"),
        pytest.param(
            {"dns_provider": None, "credentials": None, "wildcard_cert": True},
            "wildcard_cert",
            id="wildcard-without-provider",
        ),
        pytest.param({"dns_provider": "self-signed"}, "credentials", id="self-signed-credentials"),
    ],
)
def test_certificate_fields_must_agree(changes: dict[str, Any], field: str) -> None:
    assert error_locations(config(domains=[domain(**changes)])) == [("domains", 0, field)]


def test_repeated_zone_is_rejected() -> None:
    assert error_locations(config(domains=[domain(), domain()])) == [("domains",)]


def test_two_zones_work_side_by_side() -> None:
    loaded = Config.model_validate(config(domains=[domain(), domain(zone="example.net")]))
    assert [item.zone for item in loaded.domains] == ["example.com", "example.net"]


def test_ddns_record_defaults_to_home_in_the_zone() -> None:
    assert Domain.model_validate(domain()).ddns_record == "home.example.com"


def test_domain_defaults() -> None:
    loaded = Domain.model_validate(domain())
    assert loaded.wildcard_cert is True
    assert loaded.cloudflare.ddns_record is None
    assert loaded.cloudflare.ddns_update is True
    assert loaded.cloudflare.default_proxied is True


def test_ddns_record_can_be_configured() -> None:
    data = domain(cloudflare={"ddns_record": "wan.example.com"})
    assert Domain.model_validate(data).ddns_record == "wan.example.com"


def test_repeated_dns_record_name_is_rejected() -> None:
    record = {"name": "nas.example.com", "target": "192.168.1.30"}
    assert error_locations(config(dns_records=[record, record])) == [("dns_records",)]


@pytest.mark.parametrize(
    ("record", "field"),
    [
        ({"name": "nas\n.example.com", "target": "192.168.1.30"}, "name"),
        ({"name": "nas.example.com", "target": "192.168.1.30\n"}, "target"),
        ({"name": "nas.example.com", "target": "nas"}, "target"),
    ],
    ids=ascii,
)
def test_bad_dns_record_is_rejected(record: dict[str, str], field: str) -> None:
    assert error_locations(config(dns_records=[record])) == [("dns_records", 0, field)]


@pytest.mark.parametrize("value", ["1.1.1.1\n", "one.one.one.one", "1.1.1.1:53"], ids=ascii)
def test_bad_forward_target_is_rejected(value: str) -> None:
    data = config(dns_server={"listen": "192.168.1.10:5335", "forward_unknown_to": [value]})
    assert error_locations(data) == [("dns_server", "forward_unknown_to", 0)]


@pytest.mark.parametrize("code", ["USA", "U\n", 'U"', "U S", "ß", "ﬁ"], ids=ascii)
def test_bad_country_is_rejected(code: str) -> None:
    data = config(geoip={"block_countries": [code]})
    assert error_locations(data) == [("geoip", "block_countries", 0)]


def test_a_lowercase_country_is_uppercased() -> None:
    loaded = Config.model_validate(config(geoip={"block_countries": ["cn"]}))
    assert loaded.geoip.block_countries == ["CN"]


def test_crowdsec_addresses_are_required_when_enabled() -> None:
    assert error_locations(config(crowdsec={"enabled": True})) == [
        ("crowdsec", "lapi_url"),
        ("crowdsec", "syslog"),
    ]


def test_prometheus_listen_is_required_when_enabled() -> None:
    assert error_locations(config(prometheus={"enabled": True})) == [("prometheus", "listen")]


def test_enabled_prometheus_with_listen_is_accepted() -> None:
    data = config(prometheus={"enabled": True, "listen": "192.168.1.10:8405"})
    assert Config.model_validate(data).prometheus.listen == "192.168.1.10:8405"


def test_enabled_crowdsec_with_addresses_is_accepted() -> None:
    data = config(
        crowdsec={
            "enabled": True,
            "lapi_url": "http://127.0.0.1:18080",
            "syslog": "127.0.0.1:15140",
        }
    )
    assert Config.model_validate(data).crowdsec.enabled is True


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("lapi_url", "ftp://127.0.0.1:18080"),
        ("lapi_url", "http://127.0.0.1:18080/path"),
        ("lapi_url", "http://user:secret@127.0.0.1"),
        ("lapi_url", "http://127.0.0.1:0"),
        ("lapi_url", "http://127.0.0.1:18080\n"),
        ("lapi_url", 'http://127.0.0.1"'),
        ("lapi_url", "127.0.0.1:18080"),
        ("lapi_url", "http://999.1.1.1:18080"),
        ("lapi_url", "http://0x7f000001:18080"),
        ("lapi_url", "http://0x7f000001"),
        ("lapi_url", "http://127.0.0.1:65536"),
        ("lapi_url", "http://127.0.0.1:018080"),
        ("lapi_url", "http://" + ".".join(["a" * 63] * 5)),
        ("syslog", "127.0.0.1"),
        ("syslog", "127.0.0.1:15140\n"),
        # HAProxy does not start when a log target's name does not resolve, so names are refused.
        ("syslog", "crowdsec:514"),
        ("syslog", "localhost:15140"),
        ("syslog", "crowdsec.example.com:514"),
        ("syslog", "[::1]:15140"),
        ("syslog", "::1:15140"),
        ("syslog", "999.0.0.1:514"),
        ("syslog", "0x7f000001:514"),
        ("syslog", "127.0.0.1:0"),
        ("syslog", "127.0.0.1:65536"),
        ("allowlist", ["10.0.0.0/8\n"]),
        ("captcha_provider", "recaptcha\n"),
        ("appsec", "true"),
        ("appsec_url", 'http://127.0.0.1:7422"'),
        ("appsec_url", "http://127.0.0.1:7422/path"),
    ],
    ids=ascii,
)
def test_bad_crowdsec_field_is_rejected(field: str, value: object) -> None:
    locations = error_locations(config(crowdsec={field: value}))
    assert len(locations) == 1
    assert locations[0][:2] == ("crowdsec", field)


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("geoip", "provider", "ipinfo"),
        ("haproxy", "track_branch", "3.4\n"),
        ("cloudflare", "enabled", "yes"),
    ],
)
def test_bad_choice_is_rejected(section: str, key: str, value: str) -> None:
    assert error_locations(config(**{section: {key: value}})) == [(section, key)]


@pytest.mark.parametrize(
    ("cloudflare", "seconds"), [({}, 900), ({"reconcile_interval": "5m"}, 300)]
)
def test_reconcile_interval_is_read_in_seconds(cloudflare: dict[str, str], seconds: int) -> None:
    loaded = Config.model_validate(config(cloudflare=cloudflare))
    assert loaded.cloudflare.reconcile_seconds == seconds


def test_reconcile_interval_under_5m_is_rejected() -> None:
    locations = error_locations(config(cloudflare={"reconcile_interval": "299s"}))
    assert locations == [("cloudflare", "reconcile_interval")]


def test_unknown_key_is_rejected_at_every_level() -> None:
    assert error_locations(config(extra=1)) == [("extra",)]
    assert error_locations(config(network=network(extra=1))) == [("network", "extra")]
    assert error_locations(config(domains=[domain(extra=1)])) == [("domains", 0, "extra")]
