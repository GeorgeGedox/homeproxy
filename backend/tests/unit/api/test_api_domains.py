"""Domain endpoints on the real app, with a fake Cloudflare for the token check."""

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from homeproxy.store import Store, load_tree

DOMAINS = "/api/v1/domains"
NEW_TOKEN = "api-test-token-for-example-net"  # noqa: S105
BAD_TOKEN = "a-token-that-sees-no-zone"  # noqa: S105


def domain(zone: str = "example.net", **fields: Any) -> dict[str, Any]:
    """A domain body."""
    return {"zone": zone, "dns_provider": "cloudflare", "credentials": "cf-net"} | fields


def test_a_new_domain_and_its_token_are_saved_after_the_token_check(
    lab: Path, api: TestClient, cloudflare: dict[str, set[str]]
) -> None:
    """The token reaches `secrets.yaml` and never a response. The domain is pending."""
    cloudflare[NEW_TOKEN] = {"example.net"}
    response = api.post(DOMAINS, json=domain(api_token=NEW_TOKEN))

    assert response.status_code == 201
    assert NEW_TOKEN not in response.text
    assert [d["zone"] for d in api.get(DOMAINS).json()] == ["example.com", "example.net"]
    assert "example.net" not in (lab / "config" / "config.yaml").read_text()
    credential = Store(lab).load_secrets().dns_credentials["cf-net"]
    assert credential.api_token is not None
    assert credential.api_token.get_secret_value() == NEW_TOKEN
    assert api.post(DOMAINS, json=domain()).json()["error"]["code"] == "domain_exists"


@pytest.mark.parametrize(
    "plain",
    [
        pytest.param({"zone": "example.net", "wildcard_cert": False}, id="no-certificate"),
        pytest.param({"zone": "example.net", "dns_provider": "self-signed"}, id="self-signed"),
    ],
)
def test_a_domain_without_a_provider_is_saved_without_credentials(
    api: TestClient, plain: dict[str, Any]
) -> None:
    """It gets no certificate, or a self-signed one, so there is nothing to check. Credentials
    without a provider are refused, so nothing reaches `secrets.yaml` under no name."""
    with_credentials = api.post(DOMAINS, json=plain | {"env": {"EXAMPLE_TOKEN": NEW_TOKEN}})
    response = api.post(DOMAINS, json=plain)

    assert with_credentials.json()["error"]["code"] == "validation_failed"
    assert response.status_code == 201
    assert response.json()["cloudflare_off_reason"] == "example.net has no DNS provider"
    assert "example.net" in [d["zone"] for d in api.get(DOMAINS).json()]


@pytest.mark.usefixtures("cloudflare")
@pytest.mark.parametrize(
    ("body", "code"),
    [
        (domain(api_token=BAD_TOKEN), "invalid_token"),
        (domain(), "credentials_required"),
    ],
)
def test_a_domain_without_a_working_token_is_refused(
    lab: Path, api: TestClient, body: dict[str, Any], code: str
) -> None:
    """A token that cannot read the zone, or no token at all, stages and stores nothing."""
    secrets = (lab / "secrets" / "secrets.yaml").read_bytes()

    response = api.post(DOMAINS, json=body)

    assert response.json()["error"]["code"] == code
    assert response.json()["error"]["field"] == "api_token"
    assert BAD_TOKEN not in response.text
    assert api.get("/api/v1/changes").json()["changes"] == []
    assert (lab / "secrets" / "secrets.yaml").read_bytes() == secrets


def test_a_new_token_for_shared_credentials_must_read_every_zone_that_uses_them(
    lab: Path, api: TestClient, cloudflare: dict[str, set[str]]
) -> None:
    """example.com already uses cf-main. A new cf-main token that cannot read example.com would
    break its certificate, so it is refused, and one that reads both zones is saved."""
    cloudflare[NEW_TOKEN] = {"example.net"}
    secrets = lab / "secrets" / "secrets.yaml"
    before = secrets.read_bytes()
    shared = domain(credentials="cf-main", api_token=NEW_TOKEN)

    refused = api.post(DOMAINS, json=shared)

    assert refused.json()["error"]["code"] == "invalid_token"
    assert "example.com" in refused.json()["error"]["message"]
    assert secrets.read_bytes() == before
    cloudflare[NEW_TOKEN].add("example.com")
    assert api.post(DOMAINS, json=shared).status_code == 201


@pytest.mark.usefixtures("cloudflare")
def test_an_edit_uses_the_stored_token_and_keeps_the_zone(lab: Path, api: TestClient) -> None:
    """With no new token the stored one is checked. The zone cannot change."""
    edited = domain("example.com", credentials="cf-main", wildcard_cert=False)

    assert api.put(f"{DOMAINS}/example.com", json=edited).status_code == 200
    moved = api.put(f"{DOMAINS}/example.com", json=domain("example.org", credentials="cf-main"))

    assert moved.json()["error"]["code"] == "zone_changed"
    staged = load_tree(lab / "pending" / "files" / "config.yaml")
    assert staged["domains"][0]["wildcard_cert"] is False
    assert api.get(f"{DOMAINS}/example.org").status_code == 404


ENDPOINT = "http://192.168.1.10:8080/acme-endpoint-value"


def httpreq(**env: str) -> dict[str, Any]:
    """A domain body on lego's httpreq provider with new credentials."""
    return domain(dns_provider="httpreq", credentials="hr", env=env)


def test_the_provider_catalog_lists_each_provider_with_its_variables(api: TestClient) -> None:
    catalog = {p["code"]: p for p in api.get(f"{DOMAINS}/providers").json()}

    assert {"cloudflare", "route53", "httpreq"} <= set(catalog)
    assert not {"exec", "manual"} & set(catalog)
    names = [v["name"] for v in catalog["httpreq"]["credentials"]]
    assert names == ["HTTPREQ_ENDPOINT", "HTTPREQ_MODE"]


def test_a_domain_on_another_provider_is_saved_with_its_variables(
    lab: Path, api: TestClient
) -> None:
    """No token check runs, the variables reach `secrets.yaml` only, and the API says why the
    Cloudflare features are off for the domain. Its GET answer saves back unchanged."""
    response = api.post(DOMAINS, json=httpreq(HTTPREQ_ENDPOINT=ENDPOINT))

    assert response.status_code == 201
    assert ENDPOINT not in response.text
    credential = Store(lab).load_secrets().dns_credentials["hr"]
    assert credential.provider == "httpreq"
    assert credential.env["HTTPREQ_ENDPOINT"].get_secret_value() == ENDPOINT
    reasons = {d["zone"]: d["cloudflare_off_reason"] for d in api.get(DOMAINS).json()}
    assert reasons == {
        "example.com": None,
        "example.net": "example.net uses the DNS provider httpreq, not Cloudflare",
    }
    shown = api.get(f"{DOMAINS}/example.net").json()
    assert api.put(f"{DOMAINS}/example.net", json=shown).status_code == 200
    staged = load_tree(lab / "pending" / "files" / "config.yaml")
    assert "cloudflare_off_reason" not in staged["domains"][1]


@pytest.mark.parametrize(
    "body",
    [
        httpreq(HTTPREQ_ENDPOINT=ENDPOINT, LD_PRELOAD="/data/evil.so"),
        domain(dns_provider="exec", credentials="hr", env={"EXEC_PATH": "/data/evil"}),
    ],
    ids=["ld-preload", "exec"],
)
def test_credentials_that_do_not_fit_the_provider_are_refused(
    lab: Path, api: TestClient, body: dict[str, Any]
) -> None:
    """The model tests own every refused name. This checks that the API applies them."""
    secrets = (lab / "secrets" / "secrets.yaml").read_bytes()

    response = api.post(DOMAINS, json=body)

    assert (response.status_code, response.json()["error"]["code"]) == (422, "validation_failed")
    assert ENDPOINT not in response.text
    assert api.get("/api/v1/changes").json()["changes"] == []
    assert (lab / "secrets" / "secrets.yaml").read_bytes() == secrets


ENDPOINT_ENV = {"HTTPREQ_ENDPOINT": ENDPOINT}


@pytest.mark.parametrize(
    ("method", "path", "body", "code"),
    [
        ("post", "", domain(dns_provider="httpreq", credentials="cf-main"), "credentials_provider"),
        (
            "post",
            "",
            domain(dns_provider="httpreq", credentials="cf-main", env=ENDPOINT_ENV),
            "credentials_in_use",
        ),
        (
            "put",
            "/example.com",
            domain("example.com", dns_provider="httpreq", credentials="cf-main", env=ENDPOINT_ENV),
            "credentials_in_use",
        ),
        (
            "post",
            "",
            domain(
                dns_provider="route53",
                credentials="r53",
                env={"AWS_SHARED_CREDENTIALS_FILE": "/etc/passwd"},
            ),
            "path_outside_secrets",
        ),
    ],
    ids=[
        "stored-for-cloudflare",
        "shared-with-a-cloudflare-domain",
        "switch-provider-under-the-same-name",
        "path-outside-the-secrets-directory",
    ],
)
def test_credentials_that_would_break_a_domain_or_leave_the_secrets_directory_are_refused(
    lab: Path, api: TestClient, method: str, path: str, body: dict[str, Any], code: str
) -> None:
    """`secrets.yaml` changes at once while a config change is only staged, so new credentials
    under a name that a domain uses with another provider would break that live domain."""
    secrets = (lab / "secrets" / "secrets.yaml").read_bytes()

    response = api.request(method, f"{DOMAINS}{path}", json=body)

    assert (response.status_code, response.json()["error"]["code"]) == (422, code)
    assert (lab / "secrets" / "secrets.yaml").read_bytes() == secrets


def test_credentials_of_a_domain_removed_only_in_pending_keep_their_provider(
    lab: Path, api: TestClient
) -> None:
    """example.com still runs on cf-main until the removal is applied."""
    assert api.delete("/api/v1/routes/app").status_code == 204
    assert api.delete(f"{DOMAINS}/example.com").status_code == 204

    body = domain(dns_provider="httpreq", credentials="cf-main", env=ENDPOINT_ENV)
    response = api.post(DOMAINS, json=body)

    assert (response.status_code, response.json()["error"]["code"]) == (422, "credentials_in_use")


def test_a_domain_in_use_cannot_be_removed(lab: Path, api: TestClient) -> None:
    """Removal is refused while a route has a host in the zone, and staged once none does."""
    refused = api.delete(f"{DOMAINS}/example.com")
    assert (refused.status_code, refused.json()["error"]["code"]) == (409, "domain_in_use")
    assert "app" in refused.json()["error"]["message"]

    assert api.delete("/api/v1/routes/app").status_code == 204
    assert api.delete(f"{DOMAINS}/example.com").status_code == 204
    assert api.get(DOMAINS).json() == []


def test_a_nested_zone_can_be_removed_while_its_parent_covers_its_hosts(
    lab: Path, api: TestClient
) -> None:
    """A host in sub.example.com falls back to example.com, so removing the nested zone leaves no
    route without a domain."""
    with (lab / "config" / "config.yaml").open("a") as config:
        config.write(
            "  - {zone: sub.example.com, dns_provider: cloudflare, credentials: cf-main}\n"
        )
    deep = (
        (lab / "config" / "routes" / "app.yaml")
        .read_text()
        .replace("app.example.com", "app.sub.example.com")
    )
    (lab / "config" / "routes" / "deep.yaml").write_text(deep.replace("name: app", "name: deep"))

    assert api.delete(f"{DOMAINS}/sub.example.com").status_code == 204
    assert [d["zone"] for d in api.get(DOMAINS).json()] == ["example.com"]
