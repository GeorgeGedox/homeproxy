"""`POST /setup/restore`: a backup restores on a fresh install, and a hostile archive changes
nothing and leaves the setup token usable."""

import io
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

from homeproxy.auth.passwords import hash_password
from homeproxy.auth.runtime import ensure_session_key, get_runtime
from homeproxy.auth.server import BOOTSTRAP_ENV
from homeproxy.auth.tls import create_certificate
from homeproxy.store import Store
from tests.unit.api.conftest import CONFIG, PASSWORD, make_client
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


def new_key(text: str) -> str:
    """The `session_key` line of a `secrets.yaml` text."""
    return next(line for line in text.splitlines() if line.startswith("session_key"))


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
    restored = (root / "secrets" / "secrets.yaml").read_text()
    assert restored != source["secrets"], "the session key is new"
    assert restored.replace(new_key(restored), "") == source["secrets"].replace(
        new_key(source["secrets"]), ""
    )
    assert (root / "tls" / "dashboard.pem").read_text() == "restored certificate"
    assert oct((root / "secrets").stat().st_mode & 0o777) == "0o700"
    assert oct((root / "secrets" / "secrets.yaml").stat().st_mode & 0o777) == "0o600"
    assert "listen: 192.168.1.77:8443" in (root / "config" / "config.yaml").read_text()
    assert [p for p in root.iterdir() if p.name.startswith(".restore-")] == []
    assert test.post("/api/v1/auth/login", json={"password": PASSWORD}).status_code == 204
    assert test.get("/api/v1/setup/status").json() == {"setup_required": False}
    assert '"backup.restore"' in (root / "audit" / "audit.jsonl").read_text()
    assert server.should_exit is True, "the dashboard restarts to load the certificate"


def test_an_archive_from_the_install_docs_tar_command_restores_without_its_left_out_entries(
    fresh: tuple[TestClient, str, Path], source: dict[str, str]
) -> None:
    test, token, root = fresh
    members = [(info.replace(name=f"./{info.name}"), data) for info, data in base(source)]
    members += [
        file_member("./pending/config.yaml", "staged: edit"),
        file_member("./migration-backups/20261101T000000Z/secrets/secrets.yaml", "old"),
        file_member("./store.lock", ""),
        special("./generated.prev", tarfile.SYMTYPE, "releases/old"),
    ]

    assert restore(test, token, archive(members)) == 204

    assert (root / "config" / "routes" / "app.yaml").read_text() == source["route"]
    # The fresh install has its own store.lock, so the archive's is only shown to be harmless.
    for left_out in ("pending", "migration-backups", "generated.prev"):
        assert not (root / left_out).exists(), left_out


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
        "an-unknown-entry": ([*good, file_member("hooks/x", "x")], refused),
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
        pytest.param("homeproxy.api.backup_upload.MAX_UPLOAD_BYTES", 100, id="upload"),
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
    monkeypatch.setattr("homeproxy.api.backup_upload.MAX_UPLOAD_BYTES", 100)
    chunks = (body[i : i + 50] for i in range(0, len(body), 50))
    response = test.post("/api/v1/setup/restore", content=chunks, headers={HEADER: token})
    assert response.status_code == 413
    assert listing(root) == before


OLD_PASSWORD = "the password before the restore"  # noqa: S105
SNAPSHOT = "history/20261110T110600123456Z/meta.json"
MADE = 1_700_000_000


@pytest.fixture
def live(tmp_path: Path) -> tuple[TestClient, Path]:
    """A set-up install, logged in, with its own password and listen address, a certificate,
    a snapshot, an ACME file and a staged edit that a backup does not have."""
    root = tmp_path / "live"
    config = CONFIG.format(password_hash=hash_password(OLD_PASSWORD))
    files = {
        "config/config.yaml": config.replace("192.168.1.10:8443", "192.168.1.10:9443"),
        "secrets/secrets.yaml": f'version: 1\nsession_key: "{"A" * 43}="\n',
        "certs/old.example.com.pem": "old certificate",
        "history/20250101T000000000000Z/meta.json": "{}",
        "acme/account.json": "old account",
        "pending/config.yaml": "staged: edit",
    }
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    (root / "acme").chmod(0o700)
    test = make_client(root)
    login = {"password": OLD_PASSWORD}
    assert test.post("/api/v1/auth/login", json=login).status_code == 204
    return test, root


def full_backup(source: dict[str, str]) -> bytes:
    """A backup with a certificate, two snapshots and member times."""
    chain = create_certificate([]).decode()
    members = [
        *base(source),
        file_member("certs/example.com.pem", chain[chain.index("-----BEGIN CERTIFICATE") :]),
        file_member(SNAPSHOT, "{}"),
        file_member("history/20261111T110600123456Z/meta.json", "{}"),
    ]
    for offset, (info, _) in enumerate(members):
        info.mtime = MADE + offset
    return archive(members)


def test_a_check_describes_the_backup_and_changes_nothing(
    fresh: tuple[TestClient, str, Path], source: dict[str, str]
) -> None:
    test, token, root = fresh
    before = listing(root)

    response = test.post(
        "/api/v1/setup/restore/check", content=full_backup(source), headers={HEADER: token}
    )

    assert response.status_code == 200
    assert response.json() == {
        "made_at": datetime.fromtimestamp(MADE + 6, UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "config_version": 1,
        "migrated": False,
        "routes": 1,
        "domains": ["example.com"],
        "certificates": 1,
        "snapshots": 2,
        "password_set": True,
        "backup_token_set": False,
    }
    assert listing(root) == before, "no staging directory stays, and nothing moved"
    assert restore(test, token, archive(base(source))) == 204, "the token is not used up"


def test_a_check_with_a_wrong_token_counts_toward_the_throttle(
    fresh: tuple[TestClient, str, Path], source: dict[str, str]
) -> None:
    test, token, _ = fresh
    body = archive(base(source))
    wrong = {HEADER: "wrong"}
    codes = [
        test.post("/api/v1/setup/restore/check", content=body, headers=wrong) for _ in range(5)
    ]
    assert [code.status_code for code in codes] == [403] * 5
    assert restore(test, token, body) == 429


@pytest.mark.parametrize(
    ("body", "status", "code"),
    [
        pytest.param(b"not gzip", 422, "not_a_backup", id="not-an-archive"),
        pytest.param(
            archive([file_member("hooks/x", "x")]), 422, "backup_member_refused", id="bad-member"
        ),
    ],
)
@pytest.mark.parametrize("route", ["setup", "session"])
def test_both_checks_refuse_what_the_restore_refuses(
    fresh: tuple[TestClient, str, Path],
    live: tuple[TestClient, Path],
    route: str,
    body: bytes,
    status: int,
    code: str,
) -> None:
    if route == "setup":
        test, token, _ = fresh
        response = test.post("/api/v1/setup/restore/check", content=body, headers={HEADER: token})
    else:
        response = live[0].post("/api/v1/backup/check", content=body)
    assert response.status_code == status
    assert response.json()["error"]["code"] == code


def test_the_session_check_is_over_the_upload_cap_too_large(
    live: tuple[TestClient, Path], source: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("homeproxy.api.backup_upload.MAX_UPLOAD_BYTES", 100)
    response = live[0].post("/api/v1/backup/check", content=archive(base(source)))
    assert response.status_code == 413


def test_a_session_check_leaves_the_install_as_it_is(
    live: tuple[TestClient, Path], source: dict[str, str]
) -> None:
    test, root = live
    before = listing(root)
    response = test.post("/api/v1/backup/check", content=full_backup(source))
    assert response.status_code == 200
    assert response.json()["routes"] == 1
    assert listing(root) == before


def test_a_restore_on_a_set_up_install_takes_everything_from_the_backup(
    live: tuple[TestClient, Path], source: dict[str, str]
) -> None:
    test, root = live
    server = Mock(spec=uvicorn.Server, should_exit=False)
    assert isinstance(test.app, FastAPI)
    get_runtime(test.app).listen_change.serving = server
    old_key = new_key((root / "secrets" / "secrets.yaml").read_text())

    response = test.post("/api/v1/backup/restore", content=archive(base(source)))

    assert response.status_code == 204
    assert (root / "config" / "routes" / "app.yaml").read_text() == source["route"]
    config = (root / "config" / "config.yaml").read_text()
    assert "listen: 192.168.1.10:9443" in config, "the dashboard keeps its address"
    keys = {
        old_key,
        new_key(source["secrets"]),
        new_key((root / "secrets" / "secrets.yaml").read_text()),
    }
    assert len(keys) == 3, "the restore makes a new session key"
    for name in (
        "certs/old.example.com.pem",
        "history/20250101T000000000000Z",
        "acme/account.json",
    ):
        assert not (root / name).exists(), name
    for name in ("certs", "history", "acme"):
        assert list((root / name).iterdir()) == [], f"{name} is empty and still there"
    assert list((root / "pending").iterdir()) == []
    assert oct((root / "acme").stat().st_mode & 0o777) == "0o700"
    assert [p for p in root.iterdir() if p.name.startswith(".restore-")] == []
    assert '"backup.restore"' in (root / "audit" / "audit.jsonl").read_text()
    assert server.should_exit is True
    assert test.get("/api/v1/settings").status_code == 401, "the old session ended"
    old = {"password": OLD_PASSWORD}
    assert test.post("/api/v1/auth/login", json=old).status_code == 401
    assert test.post("/api/v1/auth/login", json={"password": PASSWORD}).status_code == 204


def test_a_failed_apply_after_a_restore_does_not_skip_the_restart(
    live: tuple[TestClient, Path], source: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    test, _ = live
    server = Mock(spec=uvicorn.Server, should_exit=False)
    assert isinstance(test.app, FastAPI)
    get_runtime(test.app).listen_change.serving = server

    def broken(_: object) -> None:
        raise RuntimeError("apply broke")

    monkeypatch.setattr("homeproxy.api.backup_upload.apply", broken)
    assert test.post("/api/v1/backup/restore", content=archive(base(source))).status_code == 204
    assert server.should_exit is True


def test_a_refused_restore_on_a_set_up_install_changes_nothing(
    live: tuple[TestClient, Path],
) -> None:
    test, root = live
    before = listing(root)
    response = test.post("/api/v1/backup/restore", content=b"not gzip")
    assert response.status_code == 422
    assert listing(root) == before
    assert test.get("/api/v1/settings").status_code == 200, "the session is still good"


@pytest.mark.parametrize("path", ["/api/v1/backup/check", "/api/v1/backup/restore"])
def test_the_session_routes_need_a_session_and_the_dashboards_origin(
    live: tuple[TestClient, Path], path: str
) -> None:
    root = live[1]
    assert make_client(root).post(path, content=b"x").status_code == 401
    wrong_origin = {"Origin": "https://evil.example"}
    assert live[0].post(path, content=b"x", headers=wrong_origin).status_code == 403
