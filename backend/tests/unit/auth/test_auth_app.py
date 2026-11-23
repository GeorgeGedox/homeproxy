"""Tests of the auth layer in the FastAPI app: gate, Origin check, login, logout, setup."""

import base64
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import itsdangerous.timed
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from homeproxy.api.apply import Services
from homeproxy.auth.middleware import is_api_path
from homeproxy.auth.passwords import hash_password
from homeproxy.auth.routes import ClientAddress
from homeproxy.auth.runtime import AuthRuntime, ensure_session_key, get_runtime, set_runtime
from homeproxy.auth.sessions import COOKIE_NAME
from homeproxy.certs.lego import Lego
from homeproxy.certs.service import CertificateService
from homeproxy.cli import set_password
from homeproxy.haproxy.client import HAProxyClient
from homeproxy.haproxy.pipeline import Pipeline
from homeproxy.jobs import JobRunner
from homeproxy.jobs.ddns import Ddns
from homeproxy.jobs.reconcile import Reconcile
from homeproxy.main import create_app
from homeproxy.store.files import Store

PASSWORD = "correct horse battery"  # noqa: S105
ORIGIN = "https://testserver"
LAN_CLIENT = ("192.168.1.50", 50000)
SESSION_KEY = base64.b64encode(bytes(range(32))).decode()


@pytest.fixture(scope="module")
def password_hash() -> str:
    """One argon2id hash for the module."""
    return hash_password(PASSWORD)


def write_config(root: Path, password_hash: str, ttl: str = "12h") -> None:
    """Write a minimal valid `config.yaml`."""
    config = root / "config"
    config.mkdir(parents=True, exist_ok=True)
    (config / "config.yaml").write_text(
        "# kept comment\n"
        "version: 1\n"
        "dashboard:\n"
        "  listen: 192.168.1.10:8443\n"
        f'  password_hash: "{password_hash}"\n'
        f"  session_ttl: {ttl}\n"
        "network:\n"
        "  lan_cidrs: [192.168.1.0/24]\n"
        "  haproxy_lan_ip: 192.168.1.10\n"
        "dns_server:\n"
        "  listen: 192.168.1.10:5335\n"
    )


def write_secrets(root: Path, key: str = SESSION_KEY) -> None:
    """Write a minimal valid `secrets.yaml`."""
    secrets = root / "secrets"
    secrets.mkdir(parents=True, exist_ok=True)
    (secrets / "secrets.yaml").write_text(f'version: 1\nsession_key: "{key}"\n')


def make_app(root: Path) -> tuple[FastAPI, AuthRuntime]:
    """Build the real app with a runtime on `root`, and the services setup's first apply uses.
    No HAProxy runs, so that apply fails."""
    app = create_app()
    store = Store(root)
    runtime = AuthRuntime(store)
    set_runtime(app, runtime)
    haproxy = HAProxyClient(str(root / "run" / "master.sock"), str(root / "run" / "admin.sock"))
    certificates = CertificateService(store, haproxy, Lego(root / "acme"), root / "certs")
    app.state.services = Services(
        Pipeline(store, haproxy), certificates, JobRunner(), Reconcile(store), Ddns(store)
    )
    return app, runtime


def setup_body(token: str, password: str = PASSWORD) -> dict[str, object]:
    """A valid `POST /setup` body."""
    return {
        "token": token,
        "password": password,
        "network": {"lan_cidrs": ["192.168.1.0/24"], "haproxy_lan_ip": "192.168.1.10"},
        "dns_server": {"listen": "192.168.1.10:5335"},
    }


def client(app: FastAPI, address: tuple[str, int] = LAN_CLIENT) -> TestClient:
    """A client over HTTPS that sends the dashboard's Origin on changes."""
    return TestClient(
        app, base_url="https://testserver", client=address, headers={"Origin": ORIGIN}
    )


@pytest.fixture
def configured(tmp_path: Path, password_hash: str) -> Iterator[tuple[FastAPI, Path]]:
    """An app on a data directory that has finished setup."""
    write_config(tmp_path, password_hash)
    write_secrets(tmp_path)
    app, _ = make_app(tmp_path)
    yield app, tmp_path


def login(test: TestClient, password: str = PASSWORD) -> int:
    """Post a login and return the status."""
    return test.post("/api/v1/auth/login", json={"password": password}).status_code


# Gate -----------------------------------------------------------------------------------------


def test_healthz_and_setup_status_need_no_session(configured: tuple[FastAPI, Path]) -> None:
    """Public endpoints answer without a cookie."""
    app, _ = configured
    test = client(app)
    assert test.get("/api/v1/healthz").status_code == 200
    assert test.get("/api/v1/setup/status").json() == {"setup_required": False}


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/auth/me",
        "/api/v1/routes",
        "/api/v1/anything",
        "/api/v2/x",
        "/api",
        "/api/v1/healthz/x",
        "/api/v1/auth/login/x",
    ],
)
def test_every_other_api_path_needs_a_session(configured: tuple[FastAPI, Path], path: str) -> None:
    """Deny by default: unknown API paths answer 401, not 404, without a session."""
    app, _ = configured
    response = client(app).get(path)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/api/v1/routes", True),
        ("//api/v1/routes", True),
        ("///api", True),
        ("/api", True),
        ("/apis", False),
        ("/", False),
        ("/routes", False),
        ("/_nuxt/api/x.js", False),
    ],
)
def test_api_paths_are_recognised_even_with_extra_slashes(path: str, expected: bool) -> None:
    """Extra leading slashes cannot move an API path out of the gate."""
    assert is_api_path(path) is expected


def test_unknown_path_with_a_session_is_404(configured: tuple[FastAPI, Path]) -> None:
    """The gate only adds a session check; routing still decides the rest."""
    app, _ = configured
    test = client(app)
    assert login(test) == 204
    assert test.get("/api/v1/nothing-here").status_code == 404


# Login ----------------------------------------------------------------------------------------


def test_login_sets_a_strict_cookie_and_me_works(configured: tuple[FastAPI, Path]) -> None:
    """The cookie is HttpOnly, Secure, SameSite=Strict, host-only and lives for session_ttl."""
    app, _ = configured
    test = client(app)
    response = test.post("/api/v1/auth/login", json={"password": PASSWORD})
    assert response.status_code == 204
    header = response.headers["set-cookie"]
    assert header.startswith(f"{COOKIE_NAME}=")
    lowered = header.lower()
    for attribute in ("httponly", "secure", "samesite=strict", "path=/", "max-age=43200"):
        assert attribute in lowered
    assert "domain=" not in lowered
    assert test.get("/api/v1/auth/me").json() == {"authenticated": True}


def test_wrong_password_is_401_and_sixth_attempt_is_throttled(
    configured: tuple[FastAPI, Path],
) -> None:
    """Five wrong passwords answer 401. The sixth answers 429 with Retry-After and no session,
    even with the right password. The limiter's rules are tested in `test_auth_basics`."""
    app, _ = configured
    test = client(app)
    for _ in range(5):
        response = test.post("/api/v1/auth/login", json={"password": "wrong password"})
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "invalid_password"
    throttled = test.post("/api/v1/auth/login", json={"password": PASSWORD})
    assert throttled.status_code == 429
    assert int(throttled.headers["retry-after"]) > 0
    assert COOKIE_NAME not in throttled.headers.get("set-cookie", "")


@pytest.mark.parametrize(
    "body",
    [b"", b"not json", b"[]", b'{"password": 5}', b'{"password": ""}', b'{"pw": "x"}'],
)
def test_malformed_login_is_422_without_echo(configured: tuple[FastAPI, Path], body: bytes) -> None:
    """Bad bodies get one fixed message, never the input."""
    app, _ = configured
    response = client(app).post(
        "/api/v1/auth/login", content=body, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    if body:
        assert body.decode() not in response.text


def test_login_in_setup_mode_is_409(tmp_path: Path) -> None:
    """There is no default password: before setup no password works."""
    app, _ = make_app(tmp_path)
    test = client(app)
    for password in (PASSWORD, "admin", "homeproxy", "password"):
        response = test.post("/api/v1/auth/login", json={"password": password})
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "setup_required"
    assert test.get("/api/v1/setup/status").json()["setup_required"] is True


def test_unreadable_secrets_give_503_without_content(tmp_path: Path, password_hash: str) -> None:
    """A broken secrets.yaml stops logins with a clear error, and leaks nothing."""
    write_config(tmp_path, password_hash)
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "secrets.yaml").write_text("version: 1\nsession_key: hunter2-value\n")
    app, _ = make_app(tmp_path)
    response = client(app).post("/api/v1/auth/login", json={"password": PASSWORD})
    assert response.status_code == 503
    assert "hunter2" not in response.text


def test_broken_dashboard_settings_give_503(tmp_path: Path) -> None:
    """A hash that is not argon2id is reported, not accepted, and not shown."""
    write_config(tmp_path, "$6$plainsha$" + "x" * 86)
    write_secrets(tmp_path)
    app, _ = make_app(tmp_path)
    response = client(app).post("/api/v1/auth/login", json={"password": PASSWORD})
    assert response.status_code == 503
    assert "plainsha" not in response.text


# Sessions -------------------------------------------------------------------------------------


def test_logout_revokes_the_cookie(configured: tuple[FastAPI, Path]) -> None:
    """After logout, replaying the old cookie fails."""
    app, _ = configured
    test = client(app)
    assert login(test) == 204
    cookie = test.cookies[COOKIE_NAME]
    response = test.post("/api/v1/auth/logout")
    assert response.status_code == 204
    assert f'{COOKIE_NAME}=""' in response.headers["set-cookie"]
    replay = client(app)
    response = replay.get("/api/v1/auth/me", headers={"Cookie": f"{COOKIE_NAME}={cookie}"})
    assert response.status_code == 401


def test_password_change_signs_out_every_session(configured: tuple[FastAPI, Path]) -> None:
    """`homeproxy set-password` takes effect at once: old sessions and the old password fail."""
    app, root = configured
    test = client(app)
    assert login(test) == 204
    assert test.get("/api/v1/auth/me").status_code == 200
    set_password(Store(root), "a brand new password")
    assert test.get("/api/v1/auth/me").status_code == 401
    other = client(app, ("192.168.1.52", 1))
    assert login(other) == 401
    assert login(other, "a brand new password") == 204


def test_expired_session_is_refused(
    tmp_path: Path, password_hash: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cookie older than session_ttl is refused. The signer's clock moves on, not the test's."""
    write_config(tmp_path, password_hash, ttl="1s")
    write_secrets(tmp_path)
    app, _ = make_app(tmp_path)
    test = client(app)
    assert login(test) == 204
    assert test.get("/api/v1/auth/me").status_code == 200
    later = time.time() + 2.1
    monkeypatch.setattr(itsdangerous.timed, "time", SimpleNamespace(time=lambda: later))
    assert test.get("/api/v1/auth/me").status_code == 401


def test_new_session_key_signs_out_every_session(configured: tuple[FastAPI, Path]) -> None:
    """Rotating session_key in secrets.yaml invalidates old cookies."""
    app, root = configured
    test = client(app)
    assert login(test) == 204
    write_secrets(root, base64.b64encode(bytes(32)).decode())
    assert test.get("/api/v1/auth/me").status_code == 401


# Origin ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Origin": "https://evil.example.com"},
        {"Origin": "https://testserver.evil.example"},
        {"Origin": "null"},
        {"Origin": "http://testserver"},
    ],
)
def test_changes_without_the_dashboard_origin_are_403(
    configured: tuple[FastAPI, Path], headers: dict[str, str]
) -> None:
    """Login, logout and every other change need Origin equal to the dashboard's origin."""
    app, _ = configured
    test = TestClient(app, base_url="https://testserver", client=LAN_CLIENT)
    good = client(app)
    assert login(good) == 204
    cookie = {"Cookie": f"{COOKIE_NAME}={good.cookies[COOKIE_NAME]}"}
    for path in ("/api/v1/auth/login", "/api/v1/auth/logout", "/api/v1/routes"):
        response = test.post(path, json={"password": PASSWORD}, headers={**headers, **cookie})
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "origin_mismatch"
    for method in ("PUT", "DELETE", "PATCH"):
        assert test.request(method, "/api/v1/routes/x", headers=headers).status_code == 403


def test_reads_need_no_origin(configured: tuple[FastAPI, Path]) -> None:
    """GET is not a change, so a top-level navigation still works."""
    app, _ = configured
    test = TestClient(app, base_url="https://testserver", client=LAN_CLIENT)
    assert test.get("/api/v1/setup/status").status_code == 200


# Setup ----------------------------------------------------------------------------------------


def test_setup_needs_the_token(tmp_path: Path) -> None:
    """Without the right token, setup is refused; with it, setup runs once."""
    app, runtime = make_app(tmp_path)
    assert runtime.setup_token is not None
    test = client(app)
    wrong = test.post("/api/v1/setup", json=setup_body("guess"))
    assert wrong.status_code == 403
    assert wrong.json()["error"] == {
        "code": "invalid_setup_token",
        "message": "The setup token is wrong.",
        "field": "token",
    }
    assert test.post("/api/v1/setup", json=setup_body("")).status_code == 403
    ok = test.post("/api/v1/setup", json=setup_body(runtime.setup_token.value))
    assert ok.status_code == 204
    again = test.post("/api/v1/setup", json=setup_body(runtime.setup_token.value))
    assert again.status_code == 409
    assert test.get("/api/v1/setup/status").json() == {"setup_required": False}


@pytest.mark.parametrize("address", ["8.8.8.8", "100.64.1.1", "203.0.113.5", "testclient"])
def test_setup_needs_a_private_source(tmp_path: Path, address: str) -> None:
    """The right token from a public address is refused."""
    app, runtime = make_app(tmp_path)
    assert runtime.setup_token is not None
    response = client(app, (address, 1)).post(
        "/api/v1/setup", json=setup_body(runtime.setup_token.value)
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "source_not_private"
    assert runtime.setup_required()


def test_failed_setup_releases_the_token(tmp_path: Path) -> None:
    """If setup fails after the token check, the same token works on the next try."""
    app, runtime = make_app(tmp_path)
    assert runtime.setup_token is not None
    test = client(app)
    token = runtime.setup_token.value
    assert test.post("/api/v1/setup", json=setup_body(token, "short")).status_code == 422
    assert test.post("/api/v1/setup", json=setup_body(token)).status_code == 204


def test_wrong_setup_tokens_are_throttled(tmp_path: Path) -> None:
    """Guessing the token is rate limited like the password."""
    app, runtime = make_app(tmp_path)
    assert runtime.setup_token is not None
    test = client(app)
    for _ in range(5):
        assert test.post("/api/v1/setup", json=setup_body("guess")).status_code == 403
    response = test.post("/api/v1/setup", json=setup_body(runtime.setup_token.value))
    assert response.status_code == 429


def test_setup_is_closed_once_config_exists(tmp_path: Path, password_hash: str) -> None:
    """A configured install has no setup token and refuses setup."""
    write_config(tmp_path, password_hash)
    write_secrets(tmp_path)
    app, runtime = make_app(tmp_path)
    assert runtime.setup_token is None
    response = client(app).post("/api/v1/setup", json=setup_body("anything"))
    assert response.status_code == 409


# Runtime --------------------------------------------------------------------------------------


def test_session_key_is_created_once(tmp_path: Path) -> None:
    """The first start writes secrets.yaml with a key at 0600; later starts keep it."""
    store = Store(tmp_path)
    assert ensure_session_key(store)
    first = store.secrets_path.read_bytes()
    assert (store.secrets_path.stat().st_mode & 0o777) == 0o600
    assert not ensure_session_key(store)
    assert store.secrets_path.read_bytes() == first
    assert len(base64.b64decode(store.load_secrets().session_key.get_secret_value())) == 32


def test_runtime_is_built_lazily_from_the_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An app served without `homeproxy serve` builds its runtime and prints the token."""
    monkeypatch.setenv("HOMEPROXY_DATA_DIR", str(tmp_path))
    app = create_app()
    runtime = get_runtime(app)
    assert runtime is get_runtime(app)
    assert runtime.setup_token is not None
    assert runtime.setup_token.value in capsys.readouterr().out
    assert (tmp_path / "secrets" / "secrets.yaml").exists()


def test_client_ip_comes_from_the_request(tmp_path: Path) -> None:
    """`ClientAddress` is the socket peer."""
    app, _ = make_app(tmp_path)

    @app.get("/peer")
    def peer(request: Request, address: ClientAddress) -> str:
        return address

    assert client(app, ("192.168.1.77", 9)).get("/peer").json() == "192.168.1.77"
