from typing import Any

import pytest

from homeproxy.certs.plan import CertificateSpec, plan_certificates
from homeproxy.models import Config, Route, parse_route


def make_config(*domains: dict[str, Any]) -> Config:
    return Config.model_validate(
        {
            "version": 1,
            "dashboard": {
                "listen": "192.168.1.10:8443",
                "password_hash": "$argon2id$v=19$m=65536,t=3,p=4$ZmFrZS1zYWx0$ZmFrZS1oYXNo",
            },
            "network": {"lan_cidrs": ["192.168.1.0/24"], "haproxy_lan_ip": "192.168.1.10"},
            "dns_server": {"listen": "192.168.1.10:5335"},
            "domains": [
                {"dns_provider": "cloudflare", "credentials": "cf-main", **domain}
                for domain in domains
            ],
        }
    )


def route(kind: str, *hosts: str) -> Route:
    return parse_route(
        {
            "version": 1,
            "name": "app",
            "type": kind,
            "hosts": list(hosts),
            "exposure": "lan",
            "upstreams": [{"address": "192.168.1.20:8080"}],
        }
    )


def test_each_domain_gets_one_wildcard_for_the_apex_and_one_label_below() -> None:
    config = make_config({"zone": "example.com"})

    specs = plan_certificates(config, [route("http", "example.com", "app.example.com")])

    assert specs == [
        CertificateSpec(
            "example.com", ("example.com", "*.example.com"), "example.com", "cloudflare", "cf-main"
        )
    ]


@pytest.mark.parametrize(
    ("wildcard", "host"),
    [
        pytest.param(True, "a.b.example.com", id="deeper-than-the-wildcard"),
        pytest.param(False, "app.example.com", id="wildcard-off"),
    ],
)
def test_host_outside_the_wildcard_gets_its_own_certificate(wildcard: bool, host: str) -> None:
    config = make_config({"zone": "example.com", "wildcard_cert": wildcard})

    names = [spec.name for spec in plan_certificates(config, [route("http", host)])]

    assert names == (sorted(["example.com", host]) if wildcard else [host])


@pytest.mark.parametrize(
    ("provider", "credentials"),
    [
        pytest.param("httpreq", "lab", id="lego-provider"),
        pytest.param("self-signed", None, id="self-signed"),
    ],
)
def test_host_uses_the_longest_zone_and_its_provider_and_credentials(
    provider: str, credentials: str | None
) -> None:
    config = make_config(
        {"zone": "example.com"},
        {"zone": "lab.example.com", "dns_provider": provider, "credentials": credentials},
    )

    specs = plan_certificates(config, [route("http", "a.b.lab.example.com")])

    assert (
        CertificateSpec(
            "a.b.lab.example.com",
            ("a.b.lab.example.com",),
            "lab.example.com",
            provider,
            credentials,
        )
        in specs
    )


def test_redirect_host_gets_a_certificate_as_an_http_host_does() -> None:
    config = make_config({"zone": "example.com"})
    redirect = parse_route(
        {
            "version": 1,
            "name": "www",
            "type": "redirect",
            "hosts": ["a.b.example.com"],
            "exposure": "public",
            "redirect": {"to": "https://example.com"},
        }
    )

    names = [spec.name for spec in plan_certificates(config, [redirect])]

    assert names == ["a.b.example.com", "example.com"]


@pytest.mark.parametrize(
    "item",
    [
        pytest.param(route("passthrough", "a.b.example.com"), id="passthrough-keeps-its-own"),
        pytest.param(route("http", "a.b.example.net"), id="host-outside-every-domain"),
        pytest.param(route("http", "example.org", "a.b.example.org"), id="domain-without-provider"),
    ],
)
def test_routes_that_need_no_certificate_add_none(item: Route) -> None:
    no_certificate = {"dns_provider": None, "credentials": None, "wildcard_cert": False}
    config = make_config({"zone": "example.com"}, {"zone": "example.org", **no_certificate})

    assert [spec.name for spec in plan_certificates(config, [item])] == ["example.com"]
