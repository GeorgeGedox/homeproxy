"""Typed load and save: comments survive, bad data never lands, and secrets stay out of errors."""

import base64
import logging
import secrets
import shutil
import traceback
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from pydantic import SecretStr
from ruamel.yaml.scalarstring import DoubleQuotedScalarString

from homeproxy.models import Config, Secrets, StrictModel
from homeproxy.store import (
    InvalidRouteName,
    LockNotHeld,
    ParseError,
    Store,
    ValidationFailed,
    load_tree,
    register_model_secrets,
    write_tree,
)
from homeproxy.util.redact import REDACTED, redact

DATA = Path(__file__).parents[4] / "examples" / "data"


@pytest.fixture
def store(tmp_path: Path) -> Store:
    shutil.copytree(DATA, tmp_path / "data")
    return Store(tmp_path / "data")


def files_under(path: Path) -> dict[str, bytes]:
    return {
        str(item.relative_to(path)): item.read_bytes()
        for item in sorted(path.rglob("*"))
        if item.is_file()
    }


def test_loads_seeded_files(store: Store) -> None:
    assert isinstance(store.load_config(), Config)
    assert isinstance(store.load_secrets(), Secrets)
    assert [route.name for route in store.load_routes()] == sorted(
        path.stem for path in (DATA / "config" / "routes").glob("*.yaml")
    )


def test_round_trip_tree_keeps_quoted_types_valid(store: Store) -> None:
    tree = load_tree(store.route_path("jellyfin"))
    tree["type"] = DoubleQuotedScalarString("http")
    tree["name"] = DoubleQuotedScalarString("jellyfin")
    write_tree(store.route_path("jellyfin"), tree)
    assert b'type: "http"' in store.route_path("jellyfin").read_bytes()
    assert "jellyfin" in [route.name for route in store.load_routes()]


def test_saving_an_unchanged_tree_leaves_every_file_byte_identical(store: Store) -> None:
    before = files_under(store.config_dir) | files_under(store.secrets_dir)
    with store.lock() as held:
        store.save_config(load_tree(store.config_path), held)
        store.save_secrets(load_tree(store.secrets_path), held)
    assert files_under(store.config_dir) | files_under(store.secrets_dir) == before


def test_save_edits_one_value_and_keeps_everything_else(store: Store) -> None:
    before = store.config_path.read_bytes()
    with store.lock() as held:
        tree = load_tree(store.config_path)
        tree["dashboard"]["session_ttl"] = "24h"
        store.save_config(tree, held)
    after = store.config_path.read_bytes()
    assert after == before.replace(b"session_ttl: 12h", b"session_ttl: 24h")
    assert store.load_config().dashboard.session_ttl == "24h"


def test_failed_validation_writes_nothing(store: Store) -> None:
    before = store.config_path.read_bytes()
    with store.lock() as held:
        tree = load_tree(store.config_path)
        tree["network"]["http_port"] = "not a port"
        with pytest.raises(ValidationFailed):
            store.save_config(tree, held)
    assert store.config_path.read_bytes() == before


def test_every_save_needs_a_live_lock(store: Store) -> None:
    config = load_tree(store.config_path)
    secret_tree = load_tree(store.secrets_path)
    with store.lock() as held:
        pass
    before = files_under(store.root)
    with pytest.raises(LockNotHeld):
        store.save_config(config, held)
    with pytest.raises(LockNotHeld):
        store.save_secrets(secret_tree, held)
    assert files_under(store.root) == before


def test_save_creates_missing_directories_for_a_new_data_directory(
    store: Store, tmp_path: Path
) -> None:
    fresh = Store(tmp_path / "fresh")
    with fresh.lock() as held:
        fresh.save_config(load_tree(store.config_path), held)
        fresh.save_secrets(load_tree(store.secrets_path), held)
    assert fresh.load_config() == store.load_config()
    assert fresh.secrets_dir.stat().st_mode & 0o777 == 0o700
    assert fresh.secrets_path.stat().st_mode & 0o777 == 0o600


def test_saving_secrets_tightens_a_loosened_file(store: Store) -> None:
    store.secrets_path.chmod(0o644)
    with store.lock() as held:
        store.save_secrets(load_tree(store.secrets_path), held)
    assert store.secrets_path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "name",
    [
        "../secrets/secrets",
        "../../etc/passwd",
        "a/b",
        "/etc/passwd",
        "a\n",
        "a\nb",
        "a b",
        "UPPER",
        ".hidden",
        "-leading",
        "a.yaml",
        "",
        "a\x00b",
        "a" * 64,
    ],
)
def test_hostile_route_name_cannot_reach_a_path(store: Store, name: str) -> None:
    with pytest.raises(InvalidRouteName) as caught:
        store.route_path(name)
    assert caught.value.__context__ is None
    assert name == "" or name not in str(caught.value)


def test_route_listing_skips_temp_and_hidden_files(store: Store) -> None:
    names = {path.name for path in store.route_files()}
    (store.routes_dir / ".tmp-abc.tmp").write_bytes(b"[")
    (store.routes_dir / ".jellyfin.yaml").write_bytes(b"[")
    (store.routes_dir / "notes.txt").write_bytes(b"[")
    assert {path.name for path in store.route_files()} == names
    assert len(store.load_routes()) == len(names)


def test_route_listing_without_a_routes_directory_is_empty(tmp_path: Path) -> None:
    assert Store(tmp_path).load_routes() == []


def full_text(error: BaseException) -> str:
    return "".join(traceback.format_exception(error)) + repr(vars(error))


def test_broken_secrets_yaml_error_does_not_quote_the_file(store: Store) -> None:
    secret = "topsecret" + secrets.token_hex(16)
    store.secrets_path.write_text(f'version: 1\nsession_key: "{secret}\n')
    with pytest.raises(ParseError) as caught:
        store.load_secrets()
    assert secret not in full_text(caught.value)


def test_invalid_secrets_yaml_error_does_not_quote_the_file(store: Store) -> None:
    secret = "topsecret" + secrets.token_hex(16)
    store.secrets_path.write_text(
        f"version: 1\nsession_key: {secret}\nbasic_auth:\n  files: {{{secret}: {secret}}}\n"
        f"{secret}: {secret}\n"
    )
    with pytest.raises(ValidationFailed) as caught:
        store.load_secrets()
    error = caught.value
    assert error.__cause__ is None
    assert error.__context__ is None
    assert secret not in full_text(error)
    assert "session_key" in str(error)
    assert error.errors


def test_failed_save_of_secrets_does_not_quote_the_value(store: Store) -> None:
    secret = "topsecret\n" + secrets.token_hex(16)
    tree = load_tree(store.secrets_path)
    tree["crowdsec"]["bouncer_key"] = secret
    with store.lock() as held, pytest.raises(ValidationFailed) as caught:
        store.save_secrets(tree, held)
    assert secret not in full_text(caught.value)


def test_load_secrets_registers_values_for_log_redaction(
    store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    token = "tok-" + secrets.token_hex(16)
    tree = load_tree(store.secrets_path)
    tree["dns_credentials"]["cf-main"]["api_token"] = token
    with store.lock() as held:
        store.save_secrets(tree, held)
    with caplog.at_level(logging.INFO):
        logging.getLogger("test.store").info("token is %s", token)
    assert token not in caplog.text
    assert REDACTED in caplog.text


def _secret_values(model: StrictModel) -> set[tuple[str, str]]:
    """Return (field name, value) of every `SecretStr` in a model, at any depth."""
    found: set[tuple[str, str]] = set()

    def walk(name: str, value: object) -> None:
        if isinstance(value, SecretStr):
            found.add((name, value.get_secret_value()))
        elif isinstance(value, StrictModel):
            for field in type(value).model_fields:
                walk(field, getattr(value, field))
        elif isinstance(value, dict):
            for item in value.values():
                walk(name, item)

    walk("", model)
    return found


NOT_SECRET = {"machine_id", "captcha_site_key"}


def _unique_secrets() -> Secrets:
    """Return `Secrets` with a unique value in every field, so no test can pass by accident."""
    token = secrets.token_hex(16)
    return Secrets.model_validate(
        {
            "version": 1,
            "session_key": base64.b64encode(secrets.token_bytes(32)).decode(),
            "dns_credentials": {
                "cf": {"provider": "cloudflare", "api_token": f"api-{token}"},
                "r53": {"provider": "route53", "env": {"AWS_SECRET_ACCESS_KEY": f"aws-{token}"}},
            },
            "basic_auth": {
                "route": {
                    "alice": (
                        "$6$examplesalt$xRlr.ed5yBPSJJJSBBCpquE7VZWIeHE799JD5HUcRW3skOvvrHRdSFgF6"
                        "MVeg4zaXG5VDONzRnndEiP2GQkg5."
                    )
                }
            },
            "crowdsec": {
                "bouncer_key": f"bouncer-{token}",
                "machine_id": f"machine-id-{token}",
                "machine_password": f"machine-password-{token}",
                "captcha_site_key": f"site-key-{token}",
                "captcha_secret_key": f"secret-key-{token}",
                "captcha_signing_key": f"signing-key-{token}-{'x' * 32}",
            },
            "geoip": {"maxmind_license_key": f"maxmind-{token}"},
            "notifications": {
                "targets": {"discord-main": {"url": f"https://discord.example.com/{token}"}},
                "heartbeat_url": f"https://hc.example.com/{token}",
            },
        }
    )


def test_every_secret_field_is_registered_except_the_public_ones() -> None:
    model = _unique_secrets()
    register_model_secrets(model)
    found = _secret_values(model)
    expected = NOT_SECRET | {"session_key", "api_token", "env", "url"}
    assert {name for name, _value in found} >= expected
    for name, value in found:
        if name in NOT_SECRET:
            assert redact(value) == value, name
        else:
            assert redact(value) == REDACTED, name


def test_a_notification_url_is_also_hidden_in_a_request_line() -> None:
    """HAProxy logs only the path and query of a request to a receiver behind this proxy."""
    model = _unique_secrets()
    register_model_secrets(model)
    path = urlsplit(model.notifications.heartbeat_url.get_secret_value()).path
    assert redact(f'"GET {path} HTTP/1.1"') == f'"GET {REDACTED} HTTP/1.1"'


def test_a_short_notification_path_is_not_registered_alone() -> None:
    """A path such as "/api" would garble every log line that holds it."""
    key = base64.b64encode(secrets.token_bytes(32)).decode()
    notifications = {"heartbeat_url": "https://hc.example.com/api"}
    tree = {"version": 1, "session_key": key, "notifications": notifications}
    register_model_secrets(Secrets.model_validate(tree))
    assert redact("GET /api/v1/routes") == "GET /api/v1/routes"


def test_provider_name_is_not_registered() -> None:
    register_model_secrets(_unique_secrets())
    assert redact("provider cloudflare") == "provider cloudflare"


def test_every_secret_in_the_config_model_is_registered(store: Store) -> None:
    found = _secret_values(store.load_config())
    assert "password_hash" in {name for name, _value in found}
    for name, value in found:
        assert redact(value) == REDACTED, name
