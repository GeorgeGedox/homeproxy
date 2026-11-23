"""Credential endpoints on the real app, with a fake Cloudflare for the token check."""

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from homeproxy.store import Store
from tests.integration.api.conftest import CLOUDFLARE_TOKEN

CREDENTIALS = "/api/v1/credentials"
SHORT_KEY = "abc123"
DIGITS_PASSWORD = "4815162342"  # noqa: S105
SECRET_KEY = "secret-access-key-value-0123"  # noqa: S105
SENT_SHORT = "xyz789"
SENT_DIGITS = "9081726354"
NEW_TOKEN = "api-test-token-for-credentials"  # noqa: S105
BAD_TOKEN = "a-token-that-sees-no-zone"  # noqa: S105
PATH = "/etc/secret-credentials-path"
ENDPOINT = "http://192.168.1.10:8080/acme"
SECRETS = (
    CLOUDFLARE_TOKEN,
    SHORT_KEY,
    DIGITS_PASSWORD,
    SECRET_KEY,
    SENT_SHORT,
    SENT_DIGITS,
    NEW_TOKEN,
    BAD_TOKEN,
    PATH,
)


@pytest.fixture
def aws(lab: Path, api: TestClient) -> Path:
    """`lab` with route53 credentials `r53`, which only the pending domain example.org uses, and
    httpreq credentials `hr`, which no domain uses."""
    with (lab / "secrets" / "secrets.yaml").open("a") as secrets:
        secrets.write(
            "  r53: {provider: route53, env: "
            f"{{AWS_ACCESS_KEY_ID: {SHORT_KEY}, AWS_SECRET_ACCESS_KEY: {SECRET_KEY}, "
            "AWS_REGION: us-east-1}}\n"
            "  hr: {provider: httpreq, env: "
            f"{{HTTPREQ_ENDPOINT: '{ENDPOINT}', HTTPREQ_PASSWORD: '{DIGITS_PASSWORD}'}}}}\n"
        )
    org = {"zone": "example.org", "dns_provider": "route53", "credentials": "r53"}
    assert api.post("/api/v1/domains", json=org).status_code == 201
    return lab


@pytest.mark.usefixtures("aws")
def test_the_list_shows_plain_values_and_masks_secrets(api: TestClient) -> None:
    """A value whose name marks a secret is null, also when it is short or only digits. The other
    values are shown."""
    response = api.get(CREDENTIALS)

    assert response.json() == [
        {
            "name": "cf-main",
            "provider": "cloudflare",
            "variables": {"api_token": None},
            "zones": ["example.com"],
        },
        {
            "name": "hr",
            "provider": "httpreq",
            "variables": {"HTTPREQ_ENDPOINT": ENDPOINT, "HTTPREQ_PASSWORD": None},
            "zones": [],
        },
        {
            "name": "r53",
            "provider": "route53",
            "variables": {
                "AWS_ACCESS_KEY_ID": None,
                "AWS_SECRET_ACCESS_KEY": None,
                "AWS_REGION": "us-east-1",
            },
            "zones": ["example.org"],
        },
    ]


@pytest.mark.usefixtures("aws", "cloudflare")
@pytest.mark.parametrize(
    ("method", "path", "body", "status"),
    [
        ("get", "", None, 200),
        ("put", "/cf-main", {"api_token": CLOUDFLARE_TOKEN}, 200),
        (
            "put",
            "/r53",
            {
                "env": {
                    "AWS_ACCESS_KEY_ID": SENT_SHORT,
                    "AWS_SECRET_ACCESS_KEY": SENT_DIGITS,
                    "AWS_REGION": None,
                }
            },
            200,
        ),
        ("put", "/cf-main", {"api_token": BAD_TOKEN}, 400),
        ("put", "/r53", {"env": {"AWS_SHARED_CREDENTIALS_FILE": PATH}}, 422),
        ("put", "/r53", {"env": {"AWS_ACCESS_KEY_ID": "x"}, "api_token": NEW_TOKEN}, 422),
        ("delete", "/r53", None, 409),
    ],
    ids=[
        "list",
        "replace-token",
        "replace-env",
        "invalid-token",
        "path-outside",
        "wrong-provider",
        "delete-in-use",
    ],
)
def test_no_secret_value_appears_in_a_response(
    api: TestClient, method: str, path: str, body: dict[str, Any] | None, status: int
) -> None:
    response = api.request(method, f"{CREDENTIALS}{path}", json=body)

    assert response.status_code == status
    assert not [secret for secret in SECRETS if secret in response.text]


def test_a_new_token_must_read_every_live_and_pending_zone_that_uses_it(
    lab: Path, api: TestClient, cloudflare: dict[str, set[str]]
) -> None:
    """example.com uses cf-main until its removal is applied, and example.net from the next apply,
    so a token for cf-main must read both."""
    assert api.delete("/api/v1/routes/app").status_code == 204
    assert api.delete("/api/v1/domains/example.com").status_code == 204
    cloudflare[CLOUDFLARE_TOKEN].add("example.net")
    net = {"zone": "example.net", "dns_provider": "cloudflare", "credentials": "cf-main"}
    assert api.post("/api/v1/domains", json=net).status_code == 201
    secrets = lab / "secrets" / "secrets.yaml"
    before = secrets.read_bytes()

    for zones, missing in [({"example.net"}, "example.com"), ({"example.com"}, "example.net")]:
        cloudflare[NEW_TOKEN] = zones
        refused = api.put(f"{CREDENTIALS}/cf-main", json={"api_token": NEW_TOKEN})
        assert refused.json()["error"]["code"] == "invalid_token"
        assert missing in refused.json()["error"]["message"]
        assert secrets.read_bytes() == before

    cloudflare[NEW_TOKEN] = {"example.com", "example.net"}
    saved = api.put(f"{CREDENTIALS}/cf-main", json={"api_token": NEW_TOKEN})
    assert saved.json()["zones"] == ["example.com", "example.net"]
    token = Store(lab).load_secrets().dns_credentials["cf-main"].api_token
    assert token is not None
    assert token.get_secret_value() == NEW_TOKEN


def test_a_replace_keeps_values_sent_as_null_and_removes_those_left_out(
    aws: Path, api: TestClient
) -> None:
    """The UI sends a masked secret back as null. The provider stays, and the audit log has the
    name only."""
    body = {"env": {"AWS_SECRET_ACCESS_KEY": None, "AWS_REGION": "eu-west-1"}}

    response = api.put(f"{CREDENTIALS}/r53", json=body)

    assert response.json()["variables"] == {
        "AWS_SECRET_ACCESS_KEY": None,
        "AWS_REGION": "eu-west-1",
    }
    credential = Store(aws).load_secrets().dns_credentials["r53"]
    assert credential.provider == "route53"
    stored = {n: v.get_secret_value() for n, v in credential.env.items()}
    assert stored == {"AWS_SECRET_ACCESS_KEY": SECRET_KEY, "AWS_REGION": "eu-west-1"}
    entry = api.get("/api/v1/audit").json()[0]
    assert (entry["action"], entry["details"]) == ("credentials.replace", {"name": "r53"})


@pytest.mark.parametrize(
    ("name", "status", "zones"),
    [("cf-main", 409, "example.com"), ("r53", 409, "example.org"), ("other", 404, None)],
    ids=["live", "pending", "unknown"],
)
def test_credentials_in_use_or_unknown_are_not_removed(
    aws: Path, api: TestClient, name: str, status: int, zones: str | None
) -> None:
    before = (aws / "secrets" / "secrets.yaml").read_bytes()

    response = api.delete(f"{CREDENTIALS}/{name}")

    assert response.status_code == status
    if zones is not None:
        assert response.json()["error"]["message"] == f"Domains use these credentials: {zones}."
    assert (aws / "secrets" / "secrets.yaml").read_bytes() == before


def test_unused_credentials_are_removed(aws: Path, api: TestClient) -> None:
    assert api.delete(f"{CREDENTIALS}/hr").status_code == 204

    assert [c["name"] for c in api.get(CREDENTIALS).json()] == ["cf-main", "r53"]
    assert "hr" not in Store(aws).load_secrets().dns_credentials


@pytest.mark.parametrize(
    ("body", "code"),
    [({}, "credentials_empty"), ({"env": {"AWS_TTL": None}}, "credentials_not_stored")],
    ids=["nothing", "keep-unstored"],
)
def test_a_replace_without_values_is_refused(
    aws: Path, api: TestClient, body: dict[str, Any], code: str
) -> None:
    """The result would hold no values, or keep one that is not stored."""
    before = (aws / "secrets" / "secrets.yaml").read_bytes()

    response = api.put(f"{CREDENTIALS}/r53", json=body)

    assert (response.status_code, response.json()["error"]["code"]) == (422, code)
    assert (aws / "secrets" / "secrets.yaml").read_bytes() == before
