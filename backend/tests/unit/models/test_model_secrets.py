"""Validation of `secrets.yaml`, and proof that its values stay masked."""

import base64
from typing import Any

import pytest
from pydantic import ValidationError

from homeproxy.models import Secrets

SESSION_KEY = base64.b64encode(b"k" * 32).decode()
TOKEN = "token-value-that-must-not-leak"  # noqa: S105
BASIC_HASH = f"$6$saltsalt${'a' * 86}"


def secrets(**changes: Any) -> dict[str, Any]:
    return {
        "version": 1,
        "session_key": SESSION_KEY,
        "dns_credentials": {"cf-main": {"provider": "cloudflare", "api_token": TOKEN}},
        "basic_auth": {"jellyfin": {"alice": BASIC_HASH}},
        **changes,
    }


def error_locations(data: dict[str, Any]) -> list[tuple[str | int, ...]]:
    with pytest.raises(ValidationError) as info:
        Secrets.model_validate(data)
    return [error["loc"] for error in info.value.errors()]


def test_minimal_secrets_get_empty_defaults() -> None:
    loaded = Secrets.model_validate({"version": 1, "session_key": SESSION_KEY})
    assert loaded.dns_credentials == {}
    assert loaded.basic_auth == {}
    assert loaded.crowdsec.bouncer_key.get_secret_value() == ""
    assert loaded.geoip.maxmind_license_key.get_secret_value() == ""


def test_values_stay_masked_in_repr_and_json() -> None:
    loaded = Secrets.model_validate(
        secrets(
            crowdsec={"bouncer_key": "bouncer-value", "machine_id": "machine-value"},
            geoip={"maxmind_license_key": "license-value"},
        )
    )
    outputs = [repr(loaded), str(loaded), loaded.model_dump_json(), str(loaded.model_dump())]
    for output in outputs:
        for value in (
            SESSION_KEY,
            TOKEN,
            BASIC_HASH,
            "bouncer-value",
            "machine-value",
            "license-value",
        ):
            assert value not in output


@pytest.mark.parametrize(
    "value",
    [
        "",
        "not base64",
        "a2V5",
        f"{SESSION_KEY}\n",
        base64.b64encode(b"k" * 31).decode(),
        "é" * 44,
    ],
    ids=["empty", "not-base64", "too-short", "newline", "31-bytes", "non-ascii"],
)
def test_bad_session_key_is_rejected(value: str) -> None:
    assert error_locations(secrets(session_key=value)) == [("session_key",)]


def test_missing_session_key_is_reported() -> None:
    assert error_locations({"version": 1}) == [("session_key",)]


@pytest.mark.parametrize(
    "value",
    [
        "",
        "plain",
        f"{BASIC_HASH}\n",
        f"{BASIC_HASH}\nuser evil",
        f'{BASIC_HASH}"',
        f"$5$saltsalt${'a' * 43}",
        "$2b$12$abcdefghijklmnopqrstuv",
        "$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA",
        f"$6$salt$with$extra${'a' * 86}",
        f"$6$saltsalt${'a' * 85}",
        f"$6$rounds=50001$saltsalt${'a' * 86}",
        f"$6$rounds=656000$saltsalt${'a' * 86}",
    ],
    ids=ascii,
)
def test_bad_basic_auth_hash_is_rejected(value: str) -> None:
    data = secrets(basic_auth={"jellyfin": {"alice": value}})
    assert error_locations(data) == [("basic_auth", "jellyfin", "alice")]


@pytest.mark.parametrize("rounds", ["5000", "49999", "50000"])
def test_sha512_crypt_hash_with_rounds_up_to_the_cap_is_accepted(rounds: str) -> None:
    value = f"$6$rounds={rounds}$saltsalt${'a' * 86}"
    Secrets.model_validate(secrets(basic_auth={"jellyfin": {"alice": value}}))


@pytest.mark.parametrize(
    "user", ["alice\n", 'al"ice', "al ice", "al'ice", "", "-alice", "a" * 65], ids=ascii
)
def test_bad_basic_auth_user_is_rejected(user: str) -> None:
    locations = error_locations(secrets(basic_auth={"jellyfin": {user: BASIC_HASH}}))
    assert locations == [("basic_auth", "jellyfin", user, "[key]")]


@pytest.mark.parametrize("route", ["jelly\nfin", 'jelly"fin', "Jellyfin"], ids=ascii)
def test_bad_basic_auth_route_is_rejected(route: str) -> None:
    locations = error_locations(secrets(basic_auth={route: {"alice": BASIC_HASH}}))
    assert locations == [("basic_auth", route, "[key]")]


@pytest.mark.parametrize(
    "token",
    ["", f"{TOKEN}\n", f"{TOKEN} x", f'{TOKEN}"', f"{TOKEN}'", f"{TOKEN}\x00", "é"],
    ids=ascii,
)
def test_bad_api_token_is_rejected(token: str) -> None:
    data = secrets(dns_credentials={"cf": {"provider": "cloudflare", "api_token": token}})
    assert error_locations(data) == [("dns_credentials", "cf", "api_token")]


def test_credentials_of_any_lego_provider_hold_its_documented_variables() -> None:
    env = {"AWS_ACCESS_KEY_ID": "AKIAEXAMPLE", "AWS_SECRET_ACCESS_KEY": "a b'c\"d"}
    data = secrets(dns_credentials={"r53": {"provider": "route53", "env": env}})

    loaded = Secrets.model_validate(data).dns_credentials["r53"]

    assert {name: value.get_secret_value() for name, value in loaded.env.items()} == env
    assert "AKIAEXAMPLE" not in loaded.model_dump_json()


def test_only_variables_that_hold_secrets_are_registered_for_redaction() -> None:
    """`redact` replaces every occurrence, so a TTL of `10` would garble HAProxy's answers."""
    key, key_id = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "AKIAIOSFODNN7EXAMPLE"
    env = {
        "AWS_SECRET_ACCESS_KEY": key,
        "AWS_ACCESS_KEY_ID": key_id,
        "AWS_REGION": "us-east-1",
        "AWS_TTL": "10",
        "AWS_POLLING_INTERVAL": "2",
        "AWS_MAX_RETRIES": "12345678",
        "AWS_PRIVATE_ZONE": "true",
    }
    data = secrets(dns_credentials={"r53": {"provider": "route53", "env": env}})

    credential = Secrets.model_validate(data).dns_credentials["r53"]

    assert {value.get_secret_value() for value in credential.secret_values()} == {key, key_id}


HTTPREQ = "httpreq"
ENDPOINT = {"HTTPREQ_ENDPOINT": "http://192.168.1.10:8080"}
CREDENTIAL = ("dns_credentials", "x")


@pytest.mark.parametrize(
    ("credential", "location"),
    [
        ({"provider": "exec", "env": {"EXEC_PATH": "/usr/bin/env"}}, (*CREDENTIAL, "provider")),
        ({"provider": "manual"}, (*CREDENTIAL, "provider")),
        ({"provider": "self-signed"}, (*CREDENTIAL, "provider")),
        ({"provider": "route54", "env": ENDPOINT}, (*CREDENTIAL, "provider")),
        ({"provider": "Cloudflare", "api_token": TOKEN}, (*CREDENTIAL, "provider")),
        ({"provider": HTTPREQ, "env": {**ENDPOINT, "LD_PRELOAD": "/data/x.so"}}, CREDENTIAL),
        ({"provider": HTTPREQ, "env": {"PATH": "/data"}}, CREDENTIAL),
        ({"provider": HTTPREQ, "env": {"HOME": "/data"}}, CREDENTIAL),
        ({"provider": "route53", "env": ENDPOINT}, CREDENTIAL),
        (
            {"provider": HTTPREQ, "env": {"httpreq_endpoint": "http://x"}},
            (*CREDENTIAL, "env", "httpreq_endpoint", "[key]"),
        ),
        (
            {"provider": HTTPREQ, "env": {"HTTPREQ ENDPOINT": "http://x"}},
            (*CREDENTIAL, "env", "HTTPREQ ENDPOINT", "[key]"),
        ),
        ({"provider": HTTPREQ, "api_token": TOKEN}, CREDENTIAL),
        ({"provider": "cloudflare", "env": {"CF_DNS_API_TOKEN": TOKEN}}, CREDENTIAL),
        ({"provider": "cloudflare"}, CREDENTIAL),
        (
            {"provider": HTTPREQ, "env": {"HTTPREQ_ENDPOINT": "http://x\nINJECTED=1"}},
            (*CREDENTIAL, "env", "HTTPREQ_ENDPOINT"),
        ),
        (
            {"provider": HTTPREQ, "env": {"HTTPREQ_ENDPOINT": "****"}},
            (*CREDENTIAL, "env", "HTTPREQ_ENDPOINT"),
        ),
    ],
    ids=[
        "exec",
        "manual",
        "self-signed",
        "unknown-provider",
        "provider-case",
        "ld-preload",
        "path",
        "home",
        "other-providers-variable",
        "lower-case-name",
        "name-with-space",
        "api-token-off-cloudflare",
        "cloudflare-with-env",
        "cloudflare-without-token",
        "newline-in-value",
        "masked-value",
    ],
)
def test_credentials_that_do_not_fit_their_provider_are_rejected(
    credential: dict[str, Any], location: tuple[str, ...]
) -> None:
    """Only names lego documents for the provider can reach the lego process, and the error
    never repeats one, because a secret can be pasted where a name goes."""
    with pytest.raises(ValidationError) as info:
        Secrets.model_validate(secrets(dns_credentials={"x": credential}))

    assert [error["loc"] for error in info.value.errors()] == [location]
    messages = " ".join(error["msg"] for error in info.value.errors())
    assert not any(name in messages for name in credential.get("env", {}))


def test_bad_credential_name_is_rejected() -> None:
    data = secrets(dns_credentials={"cf\nx": {"provider": "cloudflare", "api_token": TOKEN}})
    assert error_locations(data) == [("dns_credentials", "cf\nx", "[key]")]


@pytest.mark.parametrize(
    "field",
    [
        "bouncer_key",
        "machine_id",
        "machine_password",
        "captcha_site_key",
        "captcha_secret_key",
    ],
)
@pytest.mark.parametrize("character", ["\n", '"', "'", " ", "\x00"], ids=ascii)
def test_bad_crowdsec_secret_is_rejected(field: str, character: str) -> None:
    data = secrets(crowdsec={field: f"value{character}"})
    assert error_locations(data) == [("crowdsec", field)]


@pytest.mark.parametrize("mask", ["*", "**********", "*" * 32])
@pytest.mark.parametrize(
    ("build", "location"),
    [
        (
            lambda mask: {"dns_credentials": {"cf": {"provider": "cloudflare", "api_token": mask}}},
            ("dns_credentials", "cf", "api_token"),
        ),
        (lambda mask: {"crowdsec": {"bouncer_key": mask}}, ("crowdsec", "bouncer_key")),
        (lambda mask: {"crowdsec": {"machine_password": mask}}, ("crowdsec", "machine_password")),
        (
            lambda mask: {"crowdsec": {"captcha_signing_key": mask}},
            ("crowdsec", "captcha_signing_key"),
        ),
        (lambda mask: {"geoip": {"maxmind_license_key": mask}}, ("geoip", "maxmind_license_key")),
    ],
    # The session key and basic auth hashes refuse stars by their format, before this rule.
    ids=["api_token", "bouncer", "machine", "signing", "maxmind"],
)
def test_masked_value_cannot_overwrite_a_secret(
    build: Any, location: tuple[str, ...], mask: str
) -> None:
    assert error_locations(secrets(**build(mask))) == [location]


def test_token_with_a_star_is_accepted() -> None:
    data = secrets(crowdsec={"bouncer_key": "abc*def"})
    assert Secrets.model_validate(data).crowdsec.bouncer_key.get_secret_value() == "abc*def"


def test_captcha_signing_key_is_empty_or_at_least_32_bytes() -> None:
    Secrets.model_validate(secrets(crowdsec={"captcha_signing_key": ""}))
    Secrets.model_validate(secrets(crowdsec={"captcha_signing_key": "k" * 32}))
    data = secrets(crowdsec={"captcha_signing_key": "k" * 31})
    assert error_locations(data) == [("crowdsec", "captcha_signing_key")]


def test_unknown_key_is_rejected() -> None:
    assert error_locations(secrets(extra="x")) == [("extra",)]
    assert error_locations(secrets(crowdsec={"extra": "x"})) == [("crowdsec", "extra")]


def test_wrong_version_is_rejected() -> None:
    assert error_locations(secrets(version=2)) == [("version",)]
