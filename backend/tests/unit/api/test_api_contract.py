"""The OpenAPI schema and the error shape of the setup, password and settings endpoints."""

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from homeproxy.main import create_app

SECRET = "hunter2-SECRET-value"  # noqa: S105

SETUP = {
    "token": SECRET,
    "password": SECRET,
    "network": {"lan_cidrs": ["192.168.1.0/24"], "haproxy_lan_ip": SECRET},
    "dns_server": {"listen": "192.168.1.10:5335"},
    SECRET: 1,
}
SETTINGS = {
    "network": {"lan_cidrs": [SECRET], "haproxy_lan_ip": "192.168.1.10"},
    "dns_server": {"listen": "192.168.1.10:5335", SECRET: 1},
    "cloudflare": {"enabled": True},
    "geoip": {"provider": "dbip-lite"},
    "crowdsec": {"enabled": False},
}
ROUTE = {
    "version": 1,
    "name": "app",
    "type": "http",
    "hosts": [SECRET],
    "exposure": "lan",
    "upstreams": [{"address": "192.168.1.20:8080"}],
    SECRET: 1,
}
DOMAIN = {"zone": "example.com", "dns_provider": "cloudflare", "credentials": SECRET}


def test_the_endpoints_are_in_openapi() -> None:
    """The generated client gets setup, password change and settings with their bodies."""
    paths = create_app().openapi()["paths"]

    def body(path: str, method: str) -> str:
        schema = paths[path][method]["requestBody"]["content"]["application/json"]["schema"]
        ref: str = schema["$ref"]
        return ref.rsplit("/", 1)[-1]

    assert body("/api/v1/setup", "post") == "SetupRequest"
    assert body("/api/v1/auth/password", "put") == "PasswordChange"
    assert body("/api/v1/settings", "put") == "Settings"
    assert body("/api/v1/settings/dashboard", "put") == "DashboardInput"
    assert body("/api/v1/settings/keys", "put") == "KeysInput"
    assert body("/api/v1/notifications/urls", "put") == "NotificationUrlsInput"
    assert body("/api/v1/domains", "post") == "DomainInput"
    assert body("/api/v1/credentials/{name}", "put") == "CredentialInput"
    assert body("/api/v1/routes/{name}/basic-auth", "put") == "BasicAuthChange"
    for path, method in [
        ("/api/v1/routes", "get"),
        ("/api/v1/routes/validate", "post"),
        ("/api/v1/changes", "get"),
        ("/api/v1/apply", "post"),
        ("/api/v1/history", "get"),
        ("/api/v1/history/{snapshot_id}", "get"),
        ("/api/v1/history/{snapshot_id}/restore", "post"),
        ("/api/v1/status", "get"),
        ("/api/v1/certificates", "get"),
        ("/api/v1/certificates/planned", "get"),
        ("/api/v1/jobs", "get"),
        ("/api/v1/jobs/{job_id}", "get"),
        ("/api/v1/audit", "get"),
        ("/api/v1/metrics/series", "get"),
        ("/api/v1/dns/records", "get"),
        ("/api/v1/cloudflare/records", "get"),
        ("/api/v1/cloudflare/reconcile", "post"),
        ("/api/v1/cloudflare/purge", "post"),
        ("/api/v1/cloudflare/ddns/{zone}/adopt", "post"),
        ("/api/v1/cloudflare/records/{host}/adopt", "post"),
        ("/api/v1/security/decisions", "get"),
        ("/api/v1/security/decisions/counts", "get"),
        ("/api/v1/security/allowlist", "get"),
        ("/api/v1/security/allowlist", "post"),
        ("/api/v1/security/allowlist/{entry}", "delete"),
        ("/api/v1/geoip", "get"),
        ("/api/v1/settings/dashboard", "get"),
        ("/api/v1/settings/keys", "get"),
        ("/api/v1/credentials", "get"),
        ("/api/v1/notifications/urls", "get"),
        ("/api/v1/notifications/targets/{name}/test", "post"),
    ]:
        assert "200" in paths[path][method]["responses"], path
    assert "202" in paths["/api/v1/certificates/{name}/renew"]["post"]["responses"]
    assert "text/event-stream" in paths["/api/v1/logs/stream"]["get"]["responses"]["200"]["content"]
    assert {"put", "delete"} <= paths["/api/v1/routes/{name}"].keys()
    assert {"put", "delete"} <= paths["/api/v1/domains/{zone}"].keys()
    assert "204" in paths["/api/v1/credentials/{name}"]["delete"]["responses"]
    assert "delete" in paths["/api/v1/changes"]
    assert body("/api/v1/dns/records", "post") == "DnsRecord"
    assert "204" in paths["/api/v1/dns/records/{name}"]["delete"]["responses"]
    assert "200" in paths["/api/v1/settings"]["get"]["responses"]
    assert body("/api/v1/security/decisions", "post") == "NewDecision"
    assert "204" in paths["/api/v1/security/decisions/{decision_id}"]["delete"]["responses"]
    assert body("/api/v1/security/allowlist", "post") == "AllowlistEntry"
    assert body("/api/v1/geoip", "put") == "BlockList"
    assert "202" in paths["/api/v1/geoip/refresh"]["post"]["responses"]


def test_the_schema_is_not_served(api: TestClient) -> None:
    """The schema is built for the generated client only. The app does not publish it."""
    assert api.get("/openapi.json").status_code == 404


@pytest.mark.parametrize(
    ("method", "path", "body", "field"),
    [
        ("post", "/api/v1/setup", SETUP, "network.haproxy_lan_ip"),
        (
            "put",
            "/api/v1/auth/password",
            {"current_password": 7, "new_password": SECRET, SECRET: SECRET},
            "current_password",
        ),
        ("put", "/api/v1/settings", SETTINGS, "network.lan_cidrs.0"),
        ("post", "/api/v1/routes", ROUTE, "hosts.0"),
        ("post", "/api/v1/domains", DOMAIN | {SECRET: 1}, "credentials"),
        ("put", "/api/v1/routes/app/basic-auth", {"users": {SECRET: 1}}, "users.<key>"),
        ("put", "/api/v1/credentials/cf-main", {"env": {SECRET: SECRET}}, "env.<key>.[key]"),
        (
            "put",
            "/api/v1/settings/keys",
            {"crowdsec": {"captcha_signing_key": SECRET}, SECRET: 1},
            "crowdsec.captcha_signing_key",
        ),
        (
            "put",
            "/api/v1/notifications/urls",
            {"targets": {"hook": SECRET}, SECRET: 1},
            "targets.<key>",
        ),
    ],
)
def test_validation_errors_never_echo_values_or_keys(
    api: TestClient, data_dir: Path, method: str, path: str, body: dict[str, Any], field: str
) -> None:
    """A secret sent as a value or as a key never comes back, and `field` names the problem."""
    response = api.request(method.upper(), path, json=body)
    assert response.status_code == 422
    assert SECRET not in response.text
    error = response.json()["error"]
    assert error["code"] == "validation_failed"
    assert error["field"] == field
    assert "<key>" in error["message"]


@pytest.mark.parametrize(
    ("path", "body", "field", "message"),
    [
        (
            "/api/v1/routes/validate",
            {
                "version": 1,
                "name": "loop",
                "type": "redirect",
                "hosts": ["loop.example.com"],
                "exposure": "lan",
                "redirect": {"to": "https://loop.example.com/x"},
            },
            "redirect.to",
            "must not point at one of the route's hosts",
        ),
        (
            "/api/v1/domains",
            {"zone": "example.com", "wildcard_cert": True},
            "wildcard_cert",
            "a wildcard certificate needs a DNS provider",
        ),
    ],
    ids=["redirect-loop", "wildcard-without-provider"],
)
def test_a_model_rule_error_keeps_its_message_and_field(
    api: TestClient, path: str, body: dict[str, Any], field: str, message: str
) -> None:
    """Errors the models raise themselves, not pydantic's own, reach the client intact."""
    response = api.post(path, json=body)

    assert response.status_code == 422
    assert response.json()["error"] == {
        "code": "validation_failed",
        "message": f"{field}: {message}",
        "field": field,
    }


def test_a_json_syntax_error_names_no_field(api: TestClient) -> None:
    """Invalid JSON gives 422 without a character offset posing as a field name."""
    response = api.put(
        "/api/v1/settings",
        content=b'{"network": oops}',
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["field"] is None
