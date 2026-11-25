"""`POST /setup/restore`: a backup restores on a fresh install, and a hostile archive changes
nothing and leaves the setup token usable."""

import io
import tarfile
from pathlib import Path
from unittest.mock import Mock

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

from homeproxy.auth.runtime import ensure_session_key, get_runtime
from homeproxy.auth.server import BOOTSTRAP_ENV
from homeproxy.store import Store
from tests.unit.api.conftest import PASSWORD
from tests.unit.api.test_api_backup import fill
from tests.unit.api.test_api_setup import detect, setup_mode

CLIENT = "192.168.1.50"
HEADER = "X-Setup-Token"
ESCAPE = "/tmp/homeproxy-escape"  # noqa: S108 - an absolute member name, never written
SECRET_MARK = "hunter2-marker-in-a-bad-file"  # noqa: S105 - checked never to be echoed


def archive(members: list[tuple[tarfile.TarInfo, bytes]]) -> bytes:
    """A `tar.gz` of the given members."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for info, data in members:
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def file_member(name: str, data: bytes | str) -> tuple[tarfile.TarInfo, bytes]:
    """A regular file member."""
    content = data.encode() if isinstance(data, str) else data
    info = tarfile.TarInfo(name)
    info.size = len(content)
    return info, content


def special(name: str, kind: bytes, target: str = "") -> tuple[tarfile.TarInfo, bytes]:
    """A member that is not a file: a link, a device and so on."""
    info = tarfile.TarInfo(name)
    info.type = kind
    info.linkname = target
    return info, b""


@pytest.fixture
def source(lab: Path) -> dict[str, str]:
    """The text of the files of an install that has routes, a credential and certificates."""
    fill(lab)
    return {
        "config": (lab / "config" / "config.yaml").read_text(),
        "secrets": (lab / "secrets" / "secrets.yaml").read_text(),
        "route": (lab / "config" / "routes" / "app.yaml").read_text(),
    }


def base(source: dict[str, str]) -> list[tuple[tarfile.TarInfo, bytes]]:
    """The members every valid backup has."""
    return [
        file_member("config/config.yaml", source["config"]),
        file_member("config/routes/app.yaml", source["route"]),
        file_member("secrets/secrets.yaml", source["secrets"]),
        file_member("tls/dashboard.pem", "restored certificate"),
    ]


@pytest.fixture
def fresh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, str, Path]:
    """A fresh install as the image makes it: a session key, a certificate, empty directories."""
    detect(monkeypatch, tmp_path)
    monkeypatch.setenv(BOOTSTRAP_ENV, "192.168.1.77:8443")
    root = tmp_path / "fresh"
    (root / "tls").mkdir(parents=True)
    (root / "tls" / "dashboard.pem").write_text("fresh certificate")
    ensure_session_key(Store(root))
    test, token = setup_mode(root, CLIENT)
    return test, token, root


def restore(test: TestClient, token: str, body: bytes) -> int:
    """POST an archive and return the status."""
    response = test.post("/api/v1/setup/restore", content=body, headers={HEADER: token})
    return response.status_code


def listing(root: Path) -> list[str]:
    """Every path under `root`, so a test can see that nothing was added."""
    return sorted(str(path.relative_to(root)) for path in root.rglob("*"))


def test_a_backup_restores_with_its_routes_password_and_certificate(
    fresh: tuple[TestClient, str, Path], source: dict[str, str]
) -> None:
    test, token, root = fresh
    server = Mock(spec=uvicorn.Server, should_exit=False)
    assert isinstance(test.app, FastAPI)
    get_runtime(test.app).listen_change.serving = server

    assert restore(test, token, archive(base(source))) == 204

    assert (root / "config" / "routes" / "app.yaml").read_text() == source["route"]
    assert (root / "secrets" / "secrets.yaml").read_text() == source["secrets"]
    assert (root / "tls" / "dashboard.pem").read_text() == "restored certificate"
    assert oct((root / "secrets").stat().st_mode & 0o777) == "0o700"
    assert oct((root / "secrets" / "secrets.yaml").stat().st_mode & 0o777) == "0o600"
    assert "listen: 192.168.1.77:8443" in (root / "config" / "config.yaml").read_text()
    assert [p for p in root.iterdir() if p.name.startswith(".restore-")] == []
    assert test.post("/api/v1/auth/login", json={"password": PASSWORD}).status_code == 204
    assert test.get("/api/v1/setup/status").json() == {"setup_required": False}
    assert '"backup.restore"' in (root / "audit" / "audit.jsonl").read_text()
    assert server.should_exit is True, "the dashboard restarts to load the certificate"


def test_a_second_restore_after_setup_is_a_conflict(
    fresh: tuple[TestClient, str, Path], source: dict[str, str]
) -> None:
    test, token, _ = fresh
    assert restore(test, token, archive(base(source))) == 204
    assert restore(test, token, archive(base(source))) == 409


@pytest.mark.parametrize(
    ("client", "token", "status"),
    [
        pytest.param(CLIENT, "wrong", 403, id="wrong-token"),
        pytest.param("8.8.8.8", None, 403, id="not-a-private-address"),
    ],
)
def test_restore_needs_the_setup_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: dict[str, str],
    client: str,
    token: str | None,
    status: int,
) -> None:
    detect(monkeypatch, tmp_path)
    test, real = setup_mode(tmp_path / "fresh", client)
    assert restore(test, token or real, archive(base(source))) == status
    assert not (tmp_path / "fresh" / "config").exists()


def test_wrong_setup_tokens_are_throttled(
    fresh: tuple[TestClient, str, Path], source: dict[str, str]
) -> None:
    test, token, _ = fresh
    body = archive(base(source))
    assert [restore(test, "wrong", body) for _ in range(5)] == [403] * 5
    assert restore(test, token, body) == 429


type Members = list[tuple[tarfile.TarInfo, bytes]]


def refusals(source: dict[str, str]) -> dict[str, tuple[Members, str]]:
    """Each hostile or broken archive, with the error code it must get."""
    good = base(source)
    refused = "backup_member_refused"
    newer = source["config"].replace("version: 1", "version: 2", 1)
    bad_config = source["config"].replace("version: 1", f"version: 1\nunknown_key: {SECRET_MARK}")
    return {
        "a-symlink": ([*good, special("config/link", tarfile.SYMTYPE, "/etc/passwd")], refused),
        "a-hard-link": (
            [*good, special("config/hard", tarfile.LNKTYPE, "config/config.yaml")],
            refused,
        ),
        "a-device": ([*good, special("config/dev", tarfile.CHRTYPE)], refused),
        "an-absolute-path": ([*good, file_member(ESCAPE, "x")], refused),
        "a-dotdot-path": ([*good, file_member("config/../../escape", "x")], refused),
        "an-entry-outside-the-allowlist": ([*good, file_member("pending/x", "x")], refused),
        "a-path-used-twice": ([*good, file_member("config/config.yaml", "x")], refused),
        "a-file-under-a-file": ([*good, file_member("config/config.yaml/x", "x")], refused),
        "no-config": ([file_member("secrets/secrets.yaml", source["secrets"])], "not_a_backup"),
        "a-newer-version": (
            [file_member("config/config.yaml", newer), *good[1:]],
            "backup_unsupported_version",
        ),
        "an-invalid-config": (
            [file_member("config/config.yaml", bad_config), *good[1:]],
            "backup_invalid_file",
        ),
    }


REFUSED = list(refusals({"config": "", "secrets": "", "route": ""}))


@pytest.mark.parametrize("case", REFUSED)
def test_a_refused_archive_changes_nothing_and_the_token_still_works(
    fresh: tuple[TestClient, str, Path], source: dict[str, str], case: str, tmp_path: Path
) -> None:
    test, token, root = fresh
    members, code = refusals(source)[case]
    before = listing(root)

    response = test.post("/api/v1/setup/restore", content=archive(members), headers={HEADER: token})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == code
    assert SECRET_MARK not in response.text
    assert listing(root) == before
    assert not (tmp_path / "escape").exists()
    assert restore(test, token, archive(base(source))) == 204


def test_a_file_that_is_not_an_archive_is_refused(
    fresh: tuple[TestClient, str, Path], source: dict[str, str]
) -> None:
    test, token, root = fresh
    before = listing(root)
    response = test.post("/api/v1/setup/restore", content=b"not gzip", headers={HEADER: token})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "not_a_backup"
    assert listing(root) == before
    assert restore(test, token, archive(base(source))) == 204


@pytest.mark.parametrize(
    ("cap", "value"),
    [
        pytest.param("homeproxy.api.backup_restore.MAX_UPLOAD_BYTES", 100, id="upload"),
        pytest.param("homeproxy.backup.extract.MAX_EXTRACTED_BYTES", 100, id="extracted"),
        pytest.param("homeproxy.backup.extract.MAX_MEMBERS", 2, id="members"),
    ],
)
def test_an_archive_over_a_cap_is_too_large_and_the_token_still_works(
    fresh: tuple[TestClient, str, Path],
    source: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    cap: str,
    value: int,
) -> None:
    test, token, root = fresh
    body = archive(base(source))
    before = listing(root)
    with monkeypatch.context() as small:
        small.setattr(cap, value)
        response = test.post("/api/v1/setup/restore", content=body, headers={HEADER: token})
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "backup_too_large"
    assert listing(root) == before
    assert restore(test, token, body) == 204


def test_an_upload_without_a_length_is_capped_while_it_streams(
    fresh: tuple[TestClient, str, Path],
    source: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    test, token, root = fresh
    body = archive(base(source))
    before = listing(root)
    monkeypatch.setattr("homeproxy.api.backup_restore.MAX_UPLOAD_BYTES", 100)
    chunks = (body[i : i + 50] for i in range(0, len(body), 50))
    response = test.post("/api/v1/setup/restore", content=chunks, headers={HEADER: token})
    assert response.status_code == 413
    assert listing(root) == before
