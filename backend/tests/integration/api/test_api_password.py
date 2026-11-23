"""`PUT /auth/password` on the real app."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

NEW_PASSWORD = "a brand new password"  # noqa: S105


def change(api: TestClient, current: str, new: str = NEW_PASSWORD) -> int:
    """Send a password change and return the status."""
    body = {"current_password": current, "new_password": new}
    return api.put("/api/v1/auth/password", json=body).status_code


def login(client: TestClient, password: str) -> int:
    """Post a login and return the status."""
    return client.post("/api/v1/auth/login", json={"password": password}).status_code


def test_a_password_change_signs_out_other_sessions_and_keeps_this_one(
    api: TestClient, data_dir: Path, password: str
) -> None:
    """The new hash replaces the old one in place, this client gets a new cookie, and every
    other session and the old password stop working."""
    other = TestClient(
        api.app, base_url=str(api.base_url), headers={"Origin": api.headers["origin"]}
    )
    assert login(other, password) == 204

    assert change(api, password) == 204

    assert api.get("/api/v1/auth/me").status_code == 200
    assert other.get("/api/v1/auth/me").status_code == 401
    assert login(other, password) == 401
    assert login(other, NEW_PASSWORD) == 204
    text = (data_dir / "config" / "config.yaml").read_text()
    assert "# Hand-written comment that every edit keeps." in text
    assert NEW_PASSWORD not in text


def test_a_password_change_is_refused_while_changes_are_pending(
    api: TestClient, data_dir: Path, password: str
) -> None:
    """A staged `config.yaml` holds the old hash, so the change must wait for apply or discard."""
    settings = api.get("/api/v1/settings").json()
    settings["cloudflare"]["enabled"] = True
    assert api.put("/api/v1/settings", json=settings).status_code == 200
    live = data_dir / "config" / "config.yaml"
    staged = data_dir / "pending" / "files" / "config.yaml"
    before = (live.read_bytes(), staged.read_bytes())

    body = {"current_password": password, "new_password": NEW_PASSWORD}
    response = api.put("/api/v1/auth/password", json=body)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "changes_pending"
    assert (live.read_bytes(), staged.read_bytes()) == before
    assert login(api, password) == 204


@pytest.mark.parametrize(
    ("current", "new", "status", "field"),
    [
        ("not the password", NEW_PASSWORD, 403, "current_password"),
        (None, "short", 422, "new_password"),
    ],
)
def test_a_refused_change_names_the_field_and_changes_nothing(
    api: TestClient,
    data_dir: Path,
    password: str,
    current: str | None,
    new: str,
    status: int,
    field: str,
) -> None:
    """A wrong current password or a new password that breaks the rules leaves the file as is.
    `None` stands for the right current password."""
    live = data_dir / "config" / "config.yaml"
    before = live.read_bytes()
    body = {"current_password": current or password, "new_password": new}
    response = api.put("/api/v1/auth/password", json=body)
    assert response.status_code == status
    assert response.json()["error"]["field"] == field
    assert live.read_bytes() == before


def test_wrong_current_passwords_are_throttled(api: TestClient, password: str) -> None:
    """A stolen session cannot guess the password: the sixth try is refused even when right."""
    for _ in range(5):
        assert change(api, "not the password") == 403
    assert change(api, password) == 429
