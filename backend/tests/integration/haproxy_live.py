"""A real HAProxy in a container, for tests of the runtime client and of jobs that use it.

Each container has `/data` bind-mounted from a temp directory. It starts HAProxy with the same
fixed arguments as the s6 service, so a reload rereads whichever release `/data/generated` points
to. Ports are published on random loopback ports. The sockets appear in the mounted directory, so
the client connects to them from the host, which needs Docker on the same kernel.

The `certs` and `live` fixtures are in `tests/integration/conftest.py`.
"""

import os
import re
import shutil
import socket
import ssl
import subprocess
import tempfile
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx2

from homeproxy.haproxy.client import HAProxyClient, HAProxyError
from tests.conftest import wait_until

REPO_ROOT = Path(__file__).resolve().parents[3]
STARTUP_TIMEOUT = 30.0
DOCKER_TIMEOUT = 300.0
HOSTS_MAP = "hosts.map"
TRUSTED_LIST = "trusted.lst"
TRUSTED_RANGE = "10.0.0.0/8"
HOST_A = "a.example.com"

CONFIG = """\
global
    default-path parent
    stats socket /data/admin.sock mode 600 level admin expose-fd listeners

defaults
    mode http
    timeout connect 5s
    timeout client 30s
    timeout server 30s

frontend web
    bind :8080
    bind :8443 ssl crt certs/site.pem
    http-request set-var(txn.trusted) bool(1) if {{ src -f trusted.lst }}
    http-request return status 200 content-type text/plain lf-string \
"%[req.hdr(host),lower,map(hosts.map,none)] {marker}\\n"

backend pool
    server alive 127.0.0.1:8080 check inter 100ms
    server dead 127.0.0.1:9 check inter 100ms
"""


def docker(*args: str, check: bool = True, stdin: str | None = None) -> str:
    """Run the docker CLI with a fixed argument list and return its stdout, stripped. With
    `check`, a failure fails the test with the end of its stderr."""
    result = subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=DOCKER_TIMEOUT,
        input=stdin,
    )
    if check and result.returncode != 0:
        stderr = result.stderr.strip()[-2000:]
        raise AssertionError(f"docker {args[0]} exited with {result.returncode}: {stderr}")
    return result.stdout.strip()


def wait_for_haproxy(
    container: str, condition: Callable[[], object], what: str = "HAProxy to start"
) -> None:
    """Poll `condition` on the HAProxy in `container`. A failed or refused call counts as not
    yet, and a timeout shows the container log."""
    wait_until(
        condition,
        what,
        timeout=STARTUP_TIMEOUT,
        interval=0.05,
        not_yet=(httpx2.TransportError, HAProxyError, OSError),
        details=lambda: container_log(container),
    )


def container_log(container: str) -> str:
    """Return the end of a container's output, for failure messages. HAProxy writes its alerts to
    stderr, which `docker` leaves out."""
    done = subprocess.run(
        ["docker", "logs", "--tail", "40", container],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    return done.stdout + done.stderr


def haproxy_image() -> str:
    """Return the HAProxy image pinned in the Dockerfile, so tests match the product."""
    dockerfile = (REPO_ROOT / "image" / "Dockerfile").read_text()
    match = re.search(r"^ARG HAPROXY_IMAGE=(\S+)$", dockerfile, re.MULTILINE)
    assert match is not None, "image/Dockerfile no longer sets HAPROXY_IMAGE"
    return match[1]


@dataclass(frozen=True)
class PemPair:
    """A self-signed certificate and its key, generated for this test run."""

    cert: str
    key: str

    @property
    def bundle(self) -> str:
        """Return the chain and key as HAProxy's PEM bundle."""
        return self.cert + self.key

    @property
    def der(self) -> bytes:
        """Return the certificate as a TLS client sees it."""
        return ssl.PEM_cert_to_DER_cert(self.cert)


def make_cert(directory: Path, name: str) -> PemPair:
    """Generate a throwaway certificate with the openssl CLI."""
    cert_path = directory / f"{name}.crt"
    key_path = directory / f"{name}.key"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key_path),
            "-out",
            str(cert_path),
            "-subj",
            f"/CN={name}.example.com",
            "-days",
            "2",
        ],
        capture_output=True,
        check=True,
        timeout=60,
    )
    return PemPair(cert=cert_path.read_text(), key=key_path.read_text())


@dataclass
class LiveHAProxy:
    """A running HAProxy container and a client connected to its sockets."""

    data: Path
    container: str
    client: HAProxyClient
    http_port: int
    https_port: int
    release_count: int = 0

    def publish_release(self, config: str, cert: PemPair) -> None:
        """Write a new release and point `/data/generated` at it, as the pipeline does."""
        self.release_count += 1
        name = f"r{self.release_count}"
        release = self.data / "releases" / name
        (release / "conf.d").mkdir(parents=True)
        (release / "certs").mkdir()
        (release / "conf.d" / "00.cfg").write_text(config)
        (release / HOSTS_MAP).write_text(f"{HOST_A} first\n")
        (release / TRUSTED_LIST).write_text(f"{TRUSTED_RANGE}\n")
        (release / "certs" / "site.pem").write_text(cert.bundle)
        link = self.data / "generated.tmp"
        link.symlink_to(f"releases/{name}")
        link.replace(self.data / "generated")

    def get(self, host: str = HOST_A) -> str:
        """Return the answer of the HTTP frontend, which shows the map lookup for a host."""
        response = httpx2.get(
            f"http://127.0.0.1:{self.http_port}/", headers={"Host": host}, timeout=5.0
        )
        assert response.status_code == 200
        return response.text.strip()

    def served_cert(self) -> bytes:
        """Return the certificate the HTTPS frontend presents."""
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        with (
            socket.create_connection(("127.0.0.1", self.https_port), timeout=5.0) as raw,
            context.wrap_socket(raw) as tls,
        ):
            der = tls.getpeercert(binary_form=True)
        assert der is not None
        return der


@contextmanager
def started(certs: tuple[PemPair, PemPair]) -> Iterator[LiveHAProxy]:
    """Start HAProxy with the first certificate and the `v1` config, and remove it after."""
    # A short path: Unix socket paths are limited to about 100 bytes, and pytest's tmp_path
    # can be longer.
    data = Path(tempfile.mkdtemp(prefix="hp-"))
    container = f"homeproxy-test-haproxy-{uuid.uuid4().hex[:12]}"
    instance = LiveHAProxy(
        data=data,
        container=container,
        client=HAProxyClient(
            master_socket=str(data / "master.sock"),
            admin_socket=str(data / "admin.sock"),
            reload_timeout=60.0,
        ),
        http_port=0,
        https_port=0,
    )
    instance.publish_release(CONFIG.format(marker="v1"), certs[0])
    try:
        docker(
            "run",
            "--detach",
            "--name",
            container,
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--volume",
            f"{data}:/data",
            "--publish",
            "127.0.0.1::8080",
            "--publish",
            "127.0.0.1::8443",
            haproxy_image(),
            "haproxy",
            "-W",
            "-S",
            "/data/master.sock,mode,600,level,admin",
            "-f",
            "/data/generated/conf.d/",
        )
        instance.http_port = int(docker("port", container, "8080/tcp").rsplit(":", 1)[-1])
        instance.https_port = int(docker("port", container, "8443/tcp").rsplit(":", 1)[-1])
        wait_for_haproxy(instance.container, lambda: instance.get() == "first v1")
        yield instance
    finally:
        subprocess.run(
            ["docker", "rm", "--force", container], capture_output=True, check=False, timeout=60
        )
        shutil.rmtree(data, ignore_errors=True)
