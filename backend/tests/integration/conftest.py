"""Fixtures for the integration tests: the built image, run through compose.

The stack is `deploy/compose.test.yaml`. It publishes the container ports on
random loopback ports, so these tests never bind a fixed host port and several
checkouts can run them at once. Each checkout has its own compose project.

The test stack runs as compose project `<COMPOSE_PROJECT_NAME>-test`, not as the
checkout's own project, because `mise run dev` uses that one. Teardown runs
`down --volumes --remove-orphans`, which would otherwise remove the dev stack's
containers and its data volume. The image tag still comes from
`COMPOSE_PROJECT_NAME`, which is the tag `mise run image` builds. It is passed as
`HOMEPROXY_TEST_IMAGE_TAG`, because `-p` replaces `COMPOSE_PROJECT_NAME` inside
the compose file.

The API serves HTTPS with its self-signed certificate. `api_verify` is an SSL
context that trusts only that certificate, read from the container, so tests
check the certificate and the host name (it names 127.0.0.1) instead of turning
verification off.
"""

import os
import shutil
import ssl
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx2
import pytest

from tests.integration.haproxy_live import LiveHAProxy, PemPair, make_cert, started

COMPOSE_FILE = Path(__file__).resolve().parents[3] / "deploy" / "compose.test.yaml"
# Container-side ports. They are the defaults of the compose file and can be
# changed with the same variables.
API_PORT = os.environ.get("HOMEPROXY_TEST_API_PORT", "8443")
HTTP_PORT = os.environ.get("HOMEPROXY_TEST_HTTP_PORT", "80")
STARTUP_TIMEOUT = 90.0


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark every test below this directory as `integration`.

    The default pytest run (`mise run check`) deselects them, because they need
    Docker and the built image.
    """
    for item in items:
        if Path(str(item.fspath)).is_relative_to(Path(__file__).parent):
            item.add_marker(pytest.mark.integration)


@dataclass(frozen=True)
class Stack:
    """A running compose stack and the host URLs of its published ports."""

    api_url: str
    http_url: str
    api_verify: ssl.SSLContext


def checkout_project() -> str:
    """Return `COMPOSE_PROJECT_NAME`, the checkout's project and image tag, or stop the run."""
    project = os.environ.get("COMPOSE_PROJECT_NAME")
    if not project:
        pytest.exit("COMPOSE_PROJECT_NAME is not set. Run `mise run test-integration`.", 2)
    return project


def stack_project() -> str:
    """Return the compose project of the test stack, apart from the dev stack's project.

    Each pytest-xdist worker has its own session, so each one starts its own stack.
    """
    worker = os.environ.get("PYTEST_XDIST_WORKER")
    return f"{checkout_project()}-test-{worker}" if worker else f"{checkout_project()}-test"


def compose(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run `docker compose` on the test stack with a fixed argument list."""
    return subprocess.run(
        ["docker", "compose", "-p", stack_project(), "-f", str(COMPOSE_FILE), *args],
        env={**os.environ, "HOMEPROXY_TEST_IMAGE_TAG": checkout_project()},
        capture_output=True,
        text=True,
        check=check,
        timeout=180,
    )


def published_url(service: str, container_port: str, scheme: str = "http") -> str:
    """Return the loopback URL Docker chose for a published container port."""
    out = compose("port", service, container_port).stdout.strip()
    # Output is `127.0.0.1:49153`. Take the last line in case of several bindings.
    return f"{scheme}://{out.splitlines()[-1]}"


DASHBOARD_CERT = "/data/tls/dashboard.pem"


def dashboard_certificate(service: str, timeout: float) -> ssl.SSLContext:
    """Wait for the dashboard certificate in the container and trust only it.

    Only the certificate block is read. The private key before it never leaves
    the container.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        out = compose(
            "exec",
            "-T",
            service,
            "sed",
            "-n",
            "/BEGIN CERTIFICATE/,$p",
            DASHBOARD_CERT,
            check=False,
        ).stdout
        if "END CERTIFICATE" in out:
            return ssl.create_default_context(cadata=out)
        time.sleep(1.0)
    pytest.fail(f"{DASHBOARD_CERT} did not appear within {timeout:.0f}s")


def wait_for_ok(url: str, timeout: float, verify: ssl.SSLContext | bool = True) -> None:
    """Poll a URL until it answers 200, or fail the run after `timeout` seconds."""
    deadline = time.monotonic() + timeout
    last = "no answer"
    while time.monotonic() < deadline:
        try:
            response = httpx2.get(url, timeout=3.0, verify=verify)
        except httpx2.TransportError as exc:
            last = type(exc).__name__
        else:
            if response.status_code == 200:
                return
            last = f"status {response.status_code}"
        time.sleep(1.0)
    pytest.fail(f"{url} did not answer 200 within {timeout:.0f}s ({last})")


@pytest.fixture(scope="session")
def stack() -> Iterator[Stack]:
    """Start the test stack once per run and remove it, with its volume, at the end."""
    # Clear leftovers of an interrupted run in this checkout.
    compose("down", "--volumes", "--remove-orphans", check=False)
    try:
        compose("up", "--detach")
        running = Stack(
            api_url=published_url("homeproxy", API_PORT, scheme="https"),
            http_url=published_url("homeproxy", HTTP_PORT),
            api_verify=dashboard_certificate("homeproxy", STARTUP_TIMEOUT),
        )
        wait_for_ok(f"{running.api_url}/api/v1/healthz", STARTUP_TIMEOUT, running.api_verify)
        yield running
    except BaseException:
        print(compose("logs", "--no-color", "--tail", "80", check=False).stdout)
        raise
    finally:
        compose("down", "--volumes", "--remove-orphans", check=False)


@pytest.fixture(scope="session")
def test_name_prefix() -> str:
    """Prefix for the containers and networks a test starts itself, unique per checkout and
    pytest-xdist worker. It starts with `<COMPOSE_PROJECT_NAME>-test`, apart from the dev stack."""
    return stack_project()


@pytest.fixture(scope="session")
def test_compose() -> Callable[..., subprocess.CompletedProcess[str]]:
    """`compose(*args, check=True)` on the test stack's own project. Use it, never a bare
    `docker compose`, which would act on the checkout's dev stack."""
    return compose


@pytest.fixture(scope="session")
def api_url(stack: Stack) -> str:
    """Base URL of the API, for example `https://127.0.0.1:49153`."""
    return stack.api_url


@pytest.fixture(scope="session")
def api_verify(stack: Stack) -> ssl.SSLContext:
    """SSL context that trusts only the dashboard's self-signed certificate."""
    return stack.api_verify


@pytest.fixture(scope="session")
def http_url(stack: Stack) -> str:
    """Base URL of HAProxy's HTTP listener."""
    return stack.http_url


@pytest.fixture(scope="module")
def certs() -> Iterator[tuple[PemPair, PemPair]]:
    """Two unrelated certificates. Keys are never committed, so they are made here."""
    directory = Path(tempfile.mkdtemp(prefix="hp-certs-"))
    try:
        yield make_cert(directory, "first"), make_cert(directory, "second")
    finally:
        shutil.rmtree(directory)


@pytest.fixture
def live(certs: tuple[PemPair, PemPair]) -> Iterator[LiveHAProxy]:
    """HAProxy with the first certificate and the `v1` config, for this test only."""
    with started(certs) as instance:
        yield instance
