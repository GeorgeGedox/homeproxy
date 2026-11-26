"""`GET /backup` and the backup token: who gets the archive, and what it holds."""

import io
import json
import re
import tarfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from homeproxy.auth.tls import create_certificate, key_pin
from homeproxy.backup.layout import EXCLUDED, INCLUDED
from tests.unit.api.conftest import make_client

BEARER = "Authorization"


def fill(root: Path) -> None:
    """Add what a running install has besides the config: certificates, history, state."""
    files = {
        "certs/example.com.pem": "certificate",
        "acme/account.json": "account",
        "tls/dashboard.pem": "dashboard certificate",
        "history/20261110T110600123456Z/meta.json": "{}",
        "audit/audit.jsonl": "",
        "state/metrics.db": "metrics",
        "releases/r1/conf.d/10.cfg": "config",
        "pending/manifest.json": "{}",
        "migration-backups/x/config/config.yaml": "old",
        "geoip/countries.db": "countries",
        "crowdsec/spoa.yaml": "bouncer",
    }
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    (root / "generated").symlink_to("releases/r1")
    (root / "config" / "link").symlink_to("/etc/passwd")


def members(response_body: bytes) -> list[str]:
    """The member names of an archive."""
    with tarfile.open(fileobj=io.BytesIO(response_body), mode="r:gz") as archive:
        return archive.getnames()


def new_token(api: TestClient) -> str:
    """Make a token through the API."""
    response = api.post("/api/v1/backup/token")
    assert response.status_code == 201
    return str(response.json()["token"])


def download(client: TestClient, token: str) -> int:
    """The status of a download with a bearer token and no session."""
    return client.get("/api/v1/backup", headers={BEARER: f"Bearer {token}"}).status_code


def test_the_download_holds_the_included_entries_and_nothing_else(
    api: TestClient, lab: Path
) -> None:
    fill(lab)
    response = api.get("/api/v1/backup")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/gzip"
    assert re.fullmatch(
        r'attachment; filename="homeproxy-backup-[\d.]+-\d{8}T\d{6}Z\.tar\.gz"',
        response.headers["content-disposition"],
    )
    names = members(response.content)
    assert {name.split("/")[0] for name in names} == set(INCLUDED)
    assert "config/routes/app.yaml" in names
    assert "tls/dashboard.pem" in names
    assert not {name for name in names if name.split("/")[0] in EXCLUDED}
    assert "config/link" not in names, "a link is left out"
    assert list((lab / "state").glob(".backup-*")) == [], "the temp file is deleted"


def test_a_download_is_audited_with_its_address_and_auth(api: TestClient, lab: Path) -> None:
    api.get("/api/v1/backup")
    token = new_token(api)
    assert download(make_client(lab), token) == 200
    entries = [
        json.loads(line) for line in (lab / "audit" / "audit.jsonl").read_text().split("\n") if line
    ]
    downloads = [e["details"] for e in entries if e["action"] == "backup.download"]
    assert downloads == [
        {"address": "192.168.1.50", "auth": "session"},
        {"address": "192.168.1.50", "auth": "token"},
    ]


def test_only_the_hash_of_the_token_is_stored(api: TestClient, lab: Path) -> None:
    assert api.get("/api/v1/backup/token").json() == {
        "set": False,
        "created_at": None,
        "key_pin": None,
    }
    token = new_token(api)
    status = api.get("/api/v1/backup/token").json()
    assert status["set"] is True
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", status["created_at"])
    assert token not in json.dumps(status)
    stored = (lab / "secrets" / "secrets.yaml").read_text()
    assert token not in stored
    assert "backup_token_hash" in stored
    assert oct((lab / "secrets" / "secrets.yaml").stat().st_mode & 0o777) == "0o600"


def test_the_status_gives_the_key_pin_of_the_dashboard_certificate(
    api: TestClient, lab: Path
) -> None:
    fill(lab)
    (lab / "tls" / "dashboard.pem").write_bytes(create_certificate([]))
    assert api.get("/api/v1/backup/token").json()["key_pin"] == key_pin(
        lab / "tls" / "dashboard.pem"
    )


def test_the_token_downloads_the_backup_without_making_a_session(
    api: TestClient, lab: Path
) -> None:
    token = new_token(api)
    script = make_client(lab)
    response = script.get("/api/v1/backup", headers={BEARER: f"Bearer {token}"})
    assert response.status_code == 200
    assert "set-cookie" not in response.headers
    assert script.get("/api/v1/settings").status_code == 401


@pytest.mark.parametrize(
    ("method", "path"),
    [
        pytest.param("GET", "/api/v1/settings", id="settings"),
        pytest.param("GET", "/api/v1/backup/token", id="token-status"),
        pytest.param("POST", "/api/v1/backup/token", id="token-create"),
        pytest.param("DELETE", "/api/v1/backup/token", id="token-delete"),
        pytest.param("POST", "/api/v1/backup/check", id="check"),
        pytest.param("POST", "/api/v1/backup/restore", id="restore"),
        pytest.param("HEAD", "/api/v1/backup", id="head-backup"),
    ],
)
def test_the_token_is_refused_on_every_other_request(
    api: TestClient, lab: Path, method: str, path: str
) -> None:
    token = new_token(api)
    response = make_client(lab).request(method, path, headers={BEARER: f"Bearer {token}"})
    assert response.status_code == 401


def test_a_new_token_replaces_the_old_one_and_delete_removes_it(api: TestClient, lab: Path) -> None:
    first = new_token(api)
    second = new_token(api)
    script = make_client(lab)
    assert download(script, first) == 401
    assert download(script, second) == 200
    assert api.delete("/api/v1/backup/token").status_code == 204
    assert download(make_client(lab), second) == 401
    assert api.get("/api/v1/backup/token").json()["set"] is False


def test_wrong_tokens_are_throttled_even_for_the_right_token(api: TestClient, lab: Path) -> None:
    token = new_token(api)
    script = make_client(lab)
    assert [download(script, "wrong") for _ in range(5)] == [401] * 5
    refused = script.get("/api/v1/backup", headers={BEARER: f"Bearer {token}"})
    assert refused.status_code == 429
    assert int(refused.headers["retry-after"]) > 0


def test_the_download_needs_a_session_or_a_token(lab: Path) -> None:
    assert make_client(lab).get("/api/v1/backup").status_code == 401
