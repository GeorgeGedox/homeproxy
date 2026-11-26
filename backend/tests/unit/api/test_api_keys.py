"""`GET /settings/keys` and `PUT /settings/keys` on the real app: write-only, never echoed."""

import logging
import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from homeproxy.generator.crowdsec import BOUNCER_CONFIG
from homeproxy.store import Store

# Fake values, each one unique so a leak of any of them shows.
KEYS = {
    "crowdsec": {
        "bouncer_key": "bouncer-SECRET-1",  # gitleaks:allow
        "machine_id": "machine-SECRET-2",
        "machine_password": "password-SECRET-3",
        "captcha_site_key": "site-SECRET-4",
        "captcha_secret_key": "captcha-SECRET-5",
        "captcha_signing_key": "signing-SECRET-6-" + "x" * 32,
    },
    "geoip": {"maxmind_license_key": "maxmind-SECRET-7"},  # gitleaks:allow
}
VALUES = [value for section in KEYS.values() for value in section.values()]
NONE_SET = {
    "crowdsec": dict.fromkeys(KEYS["crowdsec"], False),
    "geoip": {"maxmind_license_key": False},
}


def test_keys_are_written_and_cleared_but_never_returned_logged_or_audited(
    api: TestClient, data_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A write sets every key in `secrets.yaml` (0600). Reads say only that each is set. A key
    left out stays, and `""` clears one. No value reaches an answer, a log line or the audit."""
    secrets_file = data_dir / "secrets" / "secrets.yaml"
    assert api.get("/api/v1/settings/keys").json() == NONE_SET

    with caplog.at_level(logging.DEBUG):
        written = api.put("/api/v1/settings/keys", json=KEYS)
        read = api.get("/api/v1/settings/keys")
        cleared = api.put(
            "/api/v1/settings/keys",
            json={"crowdsec": {"bouncer_key": ""}, "geoip": {}},
        )

    every_set = {section: dict.fromkeys(keys, True) for section, keys in NONE_SET.items()}
    assert written.status_code == 200
    assert written.json() == every_set
    assert read.json() == every_set
    assert cleared.json()["crowdsec"] == every_set["crowdsec"] | {"bouncer_key": False}
    assert cleared.json()["geoip"] == {"maxmind_license_key": True}
    saved = Store(data_dir).load_secrets()
    assert saved.crowdsec.bouncer_key.get_secret_value() == ""
    assert saved.crowdsec.machine_password.get_secret_value() == "password-SECRET-3"
    assert saved.geoip.maxmind_license_key.get_secret_value() == "maxmind-SECRET-7"
    assert stat.S_IMODE(secrets_file.stat().st_mode) == 0o600
    audit = (data_dir / "audit" / "audit.jsonl").read_text()
    assert "crowdsec.bouncer_key" in audit
    shown = written.text + read.text + cleared.text + caplog.text + audit
    for value in VALUES:
        assert value not in shown


def test_a_new_crowdsec_key_reaches_the_bouncer_without_an_apply(
    api: TestClient, data_dir: Path
) -> None:
    """While CrowdSec is on, a key change rewrites the bouncer's config at once, as an apply
    would. A cleared key, which the apply would refuse, leaves the bouncer's config as it is."""
    with (data_dir / "config" / "config.yaml").open("a") as config:
        config.write(
            "crowdsec:\n    enabled: true\n    lapi_url: http://127.0.0.1:18080\n"
            "    syslog: 127.0.0.1:15140\n    captcha_provider: turnstile\n"
        )
    api.put("/api/v1/settings/keys", json=KEYS)

    replaced = api.put("/api/v1/settings/keys", json={"crowdsec": {"captcha_site_key": "site-NEW"}})

    assert replaced.status_code == 200
    bouncer = (data_dir / BOUNCER_CONFIG).read_text()
    # It holds the bouncer key and the captcha secrets.
    assert stat.S_IMODE((data_dir / BOUNCER_CONFIG).stat().st_mode) == 0o600
    assert '"site_key": "site-NEW"' in bouncer
    assert '"api_key": "bouncer-SECRET-1"' in bouncer  # gitleaks:allow
    assert (
        api.put("/api/v1/settings/keys", json={"crowdsec": {"bouncer_key": ""}}).status_code == 200
    )
    assert (data_dir / BOUNCER_CONFIG).read_text() == bouncer
