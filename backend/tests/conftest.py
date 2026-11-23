"""Fixtures for every test."""

import json
import os
import shutil
import socket
import socketserver
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from homeproxy.auth import setup_detection
from homeproxy.geoip.sources import URL_ENV as GEOIP_URL_ENV
from homeproxy.haproxy.version_check import RELEASES_URL_ENV
from homeproxy.jobs.cloudflare_ranges import RANGES_URL_ENV
from homeproxy.notify.sender import THREAD_NAME
from homeproxy.util.redact import restored_secrets
from homeproxy.util.run import run

# Nothing listens on the discard port, so a job that fetches from here fails at once.
NOWHERE = "http://127.0.0.1:9"
SEND_TIMEOUT = 30.0


def pytest_sessionstart(session: pytest.Session) -> None:
    """Remove the golden HAProxy containers that a killed run of this checkout left running.

    Only the xdist controller, or a run without xdist, does it, before any test starts one. The
    label names the checkout's compose project, so other checkouts' containers stay. Integration
    containers have no such label, so a test run beside `test-integration` leaves them alone;
    that task removes its own leftovers by name.
    """
    project = os.environ.get("COMPOSE_PROJECT_NAME")
    if not project or "PYTEST_XDIST_WORKER" in os.environ or shutil.which("docker") is None:
        return
    label = f"label=homeproxy.golden={project}"
    listed = run(["docker", "ps", "--all", "--quiet", "--filter", label], check=False)
    if leftovers := listed.stdout.split():
        run(["docker", "rm", "--force", *leftovers], check=False)


@pytest.fixture(scope="session", autouse=True)
def no_internet_fetches() -> Iterator[None]:
    """Point the jobs that fetch from the internet at nowhere, for any test that starts the app."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv(RANGES_URL_ENV, NOWHERE)
        patch.setenv(RELEASES_URL_ENV, f"{NOWHERE}/")
        patch.setenv(GEOIP_URL_ENV, NOWHERE)
        yield


@pytest.fixture(autouse=True)
def no_host_port_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Setup detection sees ports 80 and 443 free, whatever listens on the test machine."""
    monkeypatch.setattr(setup_detection, "port_in_use", lambda port, address: False)


@pytest.fixture(autouse=True)
def isolated_secrets() -> Iterator[None]:
    """Forget the secrets each test registers, so none of them redacts text in a later test."""
    with restored_secrets():
        yield


def free_port() -> int:
    """Return a port on 127.0.0.1 that is free on both TCP and UDP now. The DNS service binds both
    on one number, and a TCP clash makes it wait 30 s before it tries again."""
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as tcp:
            tcp.bind(("127.0.0.1", 0))
            port: int = tcp.getsockname()[1]
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
                try:
                    udp.bind(("127.0.0.1", port))
                except OSError:
                    continue
                return port


def wait_until(
    condition: Callable[[], object],
    what: str,
    *,
    timeout: float = 30.0,
    interval: float = 0.1,
    not_yet: tuple[type[Exception], ...] = (),
    details: Callable[[], str] | None = None,
) -> None:
    """Poll `condition` until it is true, or fail the test after `timeout` seconds, naming `what`
    and adding `details()`, such as a container log. An exception in `not_yet` counts as not yet,
    and the last one is in the failure; any other exception fails the test at once."""
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while True:
        try:
            if condition():
                return
        except not_yet as error:
            last = error
        if time.monotonic() > deadline:
            message = f"timed out after {timeout:g} s waiting for {what}"
            if last is not None:
                message += f" (last error: {last!r})"
            pytest.fail(message + (f"\n{details()}" if details is not None else ""))
        time.sleep(interval)


@dataclass
class Receiver:
    """A local webhook receiver. Records each POST by path, and each GET path in `pinged`. Answers
    `statuses` in turn, then `status`. A 3xx answer points elsewhere on this receiver."""

    port: int = 0
    status: int = 204
    statuses: list[int] = field(default_factory=list)
    received: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    raw: list[str] = field(default_factory=list)
    pinged: list[str] = field(default_factory=list)

    def url(self, path: str) -> str:
        """The URL of `path` on this receiver."""
        return f"http://127.0.0.1:{self.port}{path}"

    def bodies(self, path: str) -> list[dict[str, Any]]:
        """The JSON bodies posted to `path`."""
        return [body for posted, body in self.received if posted == path]

    @staticmethod
    def wait() -> None:
        """Wait until every background notification send has ended, retries included."""
        deadline = time.monotonic() + SEND_TIMEOUT
        while any(t.name == THREAD_NAME for t in threading.enumerate()):
            assert time.monotonic() < deadline, "a notification is still being sent"
            time.sleep(0.02)


@pytest.fixture
def receiver() -> Iterator[Receiver]:
    """A `Receiver` on a random loopback port. Never Discord."""
    receiver = Receiver()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            text = self.rfile.read(int(self.headers["Content-Length"])).decode()
            receiver.raw.append(text)
            receiver.received.append((self.path, json.loads(text)))
            self.answer()

        def do_GET(self) -> None:
            receiver.pinged.append(self.path)
            self.answer()

        def answer(self) -> None:
            status = receiver.statuses.pop(0) if receiver.statuses else receiver.status
            self.send_response(status)
            if 300 <= status < 400:
                self.send_header("Location", "/moved")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    receiver.port = server.server_port
    threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True).start()
    yield receiver
    Receiver.wait()
    server.shutdown()
    server.server_close()


@pytest.fixture
def hanging_url() -> Iterator[str]:
    """A URL whose port takes the connection and never answers."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        yield f"http://127.0.0.1:{listener.getsockname()[1]}/hook"
        Receiver.wait()


@pytest.fixture
def cut_short_url() -> Iterator[str]:
    """A URL whose server closes the connection before the body it announced has arrived."""

    class CutShort(socketserver.StreamRequestHandler):
        def handle(self) -> None:
            self.rfile.readline()
            self.wfile.write(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n{")

    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), CutShort)
    threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/latest"
    server.shutdown()
    server.server_close()


@pytest.fixture
def ssh_url() -> Iterator[str]:
    """A URL whose port answers like an SSH server, not HTTP."""

    class Banner(socketserver.StreamRequestHandler):
        timeout = SEND_TIMEOUT

        def handle(self) -> None:
            self.wfile.write(b"SSH-2.0-OpenSSH_9.6\r\n")
            # Keep the connection open until the client closes it, as sshd does. Closing with the
            # POST unread sends a reset, and the client's write can fail before it reads the banner.
            self.rfile.read()

    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Banner)
    threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/hook"
    Receiver.wait()
    server.shutdown()
    server.server_close()
