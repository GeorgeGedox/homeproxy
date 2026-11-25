"""Fixtures for the API tests that run the real app in process on a temporary data directory."""

import base64
import json
import threading
from collections.abc import Iterator
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from homeproxy.api.apply import Services
from homeproxy.auth.passwords import hash_password
from homeproxy.auth.runtime import AuthRuntime, set_runtime
from homeproxy.certs.lego import Lego
from homeproxy.certs.service import CertificateService
from homeproxy.crowdsec.health import AppsecReading
from homeproxy.haproxy.client import HAProxyClient
from homeproxy.haproxy.pipeline import Pipeline
from homeproxy.jobs import JobRunner
from homeproxy.jobs.ddns import Ddns
from homeproxy.jobs.reconcile import Reconcile
from homeproxy.jobs.update_check import UpdateCheck
from homeproxy.jobs.version_check import VersionCheck
from homeproxy.logs import LogCollector
from homeproxy.logs.cloudflare_edge import CloudflareEdge
from homeproxy.logs.metrics import MetricsStore
from homeproxy.main import create_app
from homeproxy.store.files import Store

PASSWORD = "correct horse battery"  # noqa: S105
ORIGIN = "https://testserver"
LAN_CLIENT = ("192.168.1.50", 50000)

# Four-space indents, not the store's own style, so a write that changes nothing would show.
CONFIG = """\
# Hand-written comment that every edit keeps.
version: 1
dashboard:
    listen: 192.168.1.10:8443
    password_hash: "{password_hash}"
network:
    lan_cidrs: [192.168.1.0/24]  # home LAN
    haproxy_lan_ip: 192.168.1.10
dns_server:
    listen: 192.168.1.10:5335
"""


@pytest.fixture(scope="session")
def password() -> str:
    """The dashboard password of `data_dir`."""
    return PASSWORD


@pytest.fixture(scope="session")
def password_hash() -> str:
    """One argon2id hash of `PASSWORD` for the run."""
    return hash_password(PASSWORD)


@pytest.fixture
def data_dir(tmp_path: Path, password_hash: str) -> Path:
    """A data directory that has finished setup."""
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.yaml").write_text(CONFIG.format(password_hash=password_hash))
    (tmp_path / "secrets").mkdir()
    key = base64.b64encode(bytes(range(32))).decode()
    (tmp_path / "secrets" / "secrets.yaml").write_text(f'version: 1\nsession_key: "{key}"\n')
    return tmp_path


def make_client(root: Path, client: str = LAN_CLIENT[0]) -> TestClient:
    """The real app on `root`, through `client` (a LAN client by default) that sends the
    dashboard's Origin.

    No HAProxy runs, so an apply can only fail before it reaches one, and jobs stay queued. No
    version check runs either, and the update check has no source. The log collector does not
    bind its socket: tests feed it lines.
    """
    app = create_app()
    store = Store(root)
    runtime = AuthRuntime(store)
    set_runtime(app, runtime)
    haproxy = HAProxyClient(str(root / "run" / "master.sock"), str(root / "run" / "admin.sock"))
    certificates = CertificateService(store, haproxy, Lego(root / "acme"), root / "certs")
    app.state.services = Services(
        Pipeline(store, haproxy), certificates, JobRunner(), Reconcile(store), Ddns(store)
    )
    app.state.version_check = VersionCheck(lambda: haproxy.show_info().version)
    app.state.update_check = UpdateCheck(None)
    app.state.collector = LogCollector(root / "run" / "syslog.sock", lan=runtime.is_lan)
    app.state.cloudflare_edge = CloudflareEdge(root / "generated" / "cloudflare.lst")
    app.state.metrics = MetricsStore(root / "state" / "metrics.db")
    app.state.appsec = AppsecReading(store)
    return TestClient(app, base_url=ORIGIN, client=(client, 50000), headers={"Origin": ORIGIN})


@pytest.fixture
def api(data_dir: Path) -> TestClient:
    """A client with a session on the configured data directory."""
    client = make_client(data_dir)
    assert client.post("/api/v1/auth/login", json={"password": PASSWORD}).status_code == 204
    return client


def stage_config(api: TestClient) -> None:
    """Stage a change to `config.yaml`."""
    settings = api.get("/api/v1/settings").json()
    settings["dns_server"]["forward_unknown_to"] = ["192.168.1.1"]
    assert api.put("/api/v1/settings", json=settings).json()["changed"] is True


def stage_route(api: TestClient) -> None:
    """Stage a new route, which leaves `config.yaml` as it is."""
    route = {
        "version": 1,
        "name": "staged",
        "type": "http",
        "hosts": ["staged.example.com"],
        "exposure": "lan",
        "upstreams": [{"address": "192.168.1.20:8080"}],
    }
    assert api.post("/api/v1/routes", json=route).status_code == 201


CLOUDFLARE_TOKEN = "api-test-cloudflare-token"  # noqa: S105 - accepted by the fake Cloudflare
DOMAIN = """\
domains:
  - zone: example.com
    dns_provider: cloudflare
    credentials: cf-main
"""


@pytest.fixture
def lab(data_dir: Path) -> Path:
    """`data_dir` with the domain example.com, its token, and one route, `app`."""
    with (data_dir / "config" / "config.yaml").open("a") as config:
        config.write(DOMAIN)
    with (data_dir / "secrets" / "secrets.yaml").open("a") as secrets:
        credential = f"{{provider: cloudflare, api_token: {CLOUDFLARE_TOKEN}}}"
        secrets.write(f"dns_credentials:\n  cf-main: {credential}\n")
    (data_dir / "config" / "routes").mkdir()
    (data_dir / "config" / "routes" / "app.yaml").write_text(
        "# Hand-written route.\nversion: 1\nname: app\ntype: http\nhosts: [app.example.com]\n"
        "exposure: lan  # LAN only\nupstreams:\n    -   address: 192.168.1.20:8080\n"
    )
    return data_dir


@pytest.fixture
def cloudflare(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, set[str]]]:
    """A fake Cloudflare API on a random loopback port. It yields the zones each token can read,
    first only example.com for `CLOUDFLARE_TOKEN`. Add tokens to it in a test."""
    granted = {CLOUDFLARE_TOKEN: {"example.com"}}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            token = self.headers.get("Authorization", "").removeprefix("Bearer ")
            query = parse_qs(urlsplit(self.path).query)
            visible = granted.get(token, set())
            result = [{"id": "zone-1", "name": n} for n in query.get("name", []) if n in visible]
            body = json.dumps({"success": True, "result": result}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("CLOUDFLARE_BASE_URL", f"http://127.0.0.1:{server.server_port}")
    yield granted
    server.shutdown()
    server.server_close()


MACHINE_ID = "homeproxy-ui"
MACHINE_PASSWORD = "api-test-machine-password"  # noqa: S105 - accepted by the fake LAPI
TOKEN = "api-test-lapi-token"  # noqa: S105
BOUNCER_KEY = "api-test-bouncer-key"
PULLED_AT = "2026-11-20T10:00:00Z"
COMMUNITY_BAN = {
    "id": 7,
    "type": "ban",
    "scope": "Ip",
    "value": "203.0.113.7",
    "origin": "CAPI",
    "scenario": "crowdsecurity/http-probing",
    "duration": "3h59m50s",
}


class FakeLapi:
    """Answers the LAPI calls of the machine and the bouncer, and AppSec's, as CrowdSec v1.8.2
    does."""

    def __init__(self) -> None:
        self.decisions: list[dict[str, Any]] = [dict(COMMUNITY_BAN)]
        self.added: list[dict[str, Any]] = []
        self.listing_fails = False
        self.appsec_refuses = False

    def answer(self, method: str, path: str, body: Any, headers: Message) -> tuple[int, Any]:
        if path == "/v1/decisions":
            ok = headers.get("X-Api-Key") == BOUNCER_KEY
            return (200, None) if ok else (403, {"message": "access forbidden"})
        if path == "/":
            if self.appsec_refuses or headers.get("X-Crowdsec-Appsec-Api-Key") != BOUNCER_KEY:
                return 401, ""
            return 200, {"action": "allow", "http_status": 200}
        auth = headers.get("Authorization", "")
        if path == "/v1/watchers/login":
            ok = body == {"machine_id": MACHINE_ID, "password": MACHINE_PASSWORD}
            return (200, {"token": TOKEN}) if ok else (401, {"message": "incorrect Username"})
        if auth != f"Bearer {TOKEN}":
            return 401, {"message": "no token"}
        if method == "GET":
            if self.listing_fails:
                return 500, {"message": "listing timed out"}
            return 200, [{"created_at": PULLED_AT, "decisions": self.decisions}]
        if method == "POST":
            self.added += body
            return 201, ["1"]
        decision_id = int(path.rsplit("/", 1)[1])
        if decision_id not in [d["id"] for d in self.decisions]:
            return 500, {"message": "decision with id 99 not found"}
        self.decisions = [d for d in self.decisions if d["id"] != decision_id]
        return 200, {"nbDeleted": "1"}


@pytest.fixture
def lapi() -> Iterator[tuple[FakeLapi, str]]:
    """A fake LAPI on a random loopback port, and its URL."""
    fake = FakeLapi()

    class Handler(BaseHTTPRequestHandler):
        def _handle(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length)) if length else None
            path = self.path.split("?", 1)[0]
            status, answer = fake.answer(self.command, path, body, self.headers)
            data = json.dumps(answer).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = do_DELETE = _handle

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield fake, f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


def enable_crowdsec(
    data_dir: Path,
    lapi_url: str,
    password: str = MACHINE_PASSWORD,
    bouncer_key: str = BOUNCER_KEY,
    appsec: bool = False,
) -> None:
    """Turn CrowdSec on at `lapi_url`. With `appsec`, AppSec is the fake at the same address."""
    with (data_dir / "config" / "config.yaml").open("a") as config:
        config.write(f"crowdsec:\n  enabled: true\n  lapi_url: {lapi_url}\n")
        config.write("  syslog: 127.0.0.1:5140\n")
        if appsec:
            config.write(f"  appsec: true\n  appsec_url: {lapi_url}\n")
    with (data_dir / "secrets" / "secrets.yaml").open("a") as secrets:
        secrets.write(f"crowdsec:\n  machine_id: {MACHINE_ID}\n  machine_password: {password}\n")
        secrets.write(f"  bouncer_key: {bouncer_key}\n")
