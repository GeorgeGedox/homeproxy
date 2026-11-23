"""A dashboard server on a real socket, run in a thread, and the files it reads."""

import base64
import http.client
import json
import ssl
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import uvicorn

from homeproxy.auth.listen import ListenChange
from homeproxy.auth.passwords import hash_password
from homeproxy.auth.runtime import AuthRuntime, set_runtime
from homeproxy.auth.server import (
    ListenPlan,
    build_config,
)
from homeproxy.auth.tls import ensure_certificate
from homeproxy.main import create_app
from homeproxy.store.files import Store

PASSWORD = "correct horse battery"  # noqa: S105


def write_files(root: Path, lan: str = "127.0.0.0/8", listen: str = "127.0.0.1:8443") -> None:
    """Write a configured data directory with `PASSWORD` and the given LAN range."""
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "config" / "config.yaml").write_text(
        "version: 1\n"
        "dashboard:\n"
        f"  listen: {listen}\n"
        f'  password_hash: "{hash_password(PASSWORD)}"\n'
        "network:\n"
        f"  lan_cidrs: [{lan}]\n"
        "  haproxy_lan_ip: 192.168.1.10\n"
        "dns_server:\n"
        "  listen: 192.168.1.10:5335\n"
    )
    (root / "secrets").mkdir(parents=True, exist_ok=True)
    key = base64.b64encode(bytes(range(32))).decode()
    (root / "secrets" / "secrets.yaml").write_text(f'version: 1\nsession_key: "{key}"\n')


@contextmanager
def running(
    root: Path, confirm_seconds: float | None = None
) -> Iterator[tuple[int, ssl.SSLContext]]:
    """Serve the real app over TLS on 127.0.0.1 and a free port, as `homeproxy serve` does. Yield
    the port and a client context that trusts only the dashboard certificate. `confirm_seconds`
    shortens the window of a new listen address."""
    certificate = root / "tls" / "dashboard.pem"
    ensure_certificate(certificate, ["127.0.0.1"])
    app = create_app()
    runtime = AuthRuntime(Store(root))
    if confirm_seconds is not None:
        runtime.listen_change = ListenChange(runtime.store, runtime.source_policy, confirm_seconds)
    set_runtime(app, runtime)
    plan = ListenPlan("127.0.0.1", 0, setup_mode=False, reason="test")
    config = build_config(plan, str(certificate), app, runtime.source_policy)
    config.log_level = "warning"
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        assert time.monotonic() < deadline, "server did not start"
        assert thread.is_alive(), "server thread died"
        time.sleep(0.05)
    port = server.servers[0].sockets[0].getsockname()[1]
    pem = certificate.read_text()
    context = ssl.create_default_context(cadata=pem[pem.index("-----BEGIN CERTIFICATE-----") :])
    try:
        yield port, context
    finally:
        server.should_exit = True
        thread.join(15)


def request(
    port: int,
    context: ssl.SSLContext,
    method: str,
    path: str,
    body: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
) -> http.client.HTTPResponse:
    """Send one HTTPS request with certificate and host name checks on."""
    connection = http.client.HTTPSConnection("127.0.0.1", port, context=context, timeout=10)
    sent = {"Origin": f"https://127.0.0.1:{port}", **(headers or {})}
    data = json.dumps(body).encode() if body is not None else None
    if data is not None:
        sent["Content-Type"] = "application/json"
    connection.request(method, path, body=data, headers=sent)
    response = connection.getresponse()
    response.read()
    return response
