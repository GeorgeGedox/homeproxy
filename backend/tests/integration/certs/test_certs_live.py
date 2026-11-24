"""Certificates end to end: the real lego, a local ACME CA, a fake Cloudflare and a real HAProxy.

- lego is the binary from the image pinned in `image/Dockerfile`, run on the host.
- Pebble is Let's Encrypt's test CA. It skips challenge validation, so it needs no DNS.
- `FakeCloudflare` serves the Cloudflare API calls lego makes, and answers DNS for its zones on a
  random UDP port, which stands in for the public resolver. lego's check of the authoritative
  name servers is turned off, because it would need port 53.
- HAProxy runs from the pinned image with `/data` bind-mounted, and loads `crt /data/certs/` as
  the generated config does.

The fixtures live in this file because `mypy` treats a second `conftest.py` as a duplicate.
"""

import json
import os
import re
import shutil
import socket
import ssl
import struct
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from cryptography import x509
from pydantic import TypeAdapter

from homeproxy.certs.deploy import Delivery
from homeproxy.certs.errors import CertificateError, TokenError
from homeproxy.certs.lego import Lego
from homeproxy.certs.providers import CATALOG_FILE, LegoProvider, read_catalog, removed
from homeproxy.certs.service import CertificateService
from homeproxy.generator.certificate import ensure_default_certificate
from homeproxy.haproxy.client import HAProxyClient
from homeproxy.jobs.certs import submit_issue
from homeproxy.jobs.models import JobState
from homeproxy.jobs.runner import JobRunner
from homeproxy.store import load_tree, write_tree
from homeproxy.store.files import Store
from tests.conftest import wait_until
from tests.integration.haproxy_live import docker

REPO_ROOT = Path(__file__).resolve().parents[4]
EXAMPLES = REPO_ROOT / "examples" / "data"
PEBBLE_IMAGE = (
    "ghcr.io/letsencrypt/pebble:2.10.0"
    "@sha256:68cf1ec8a8db96f64244d5f559c448bc8e54f2934e0dd53a414eabffda7a6f22"
)
NINETY_DAYS = 7776000
TIMEOUT = 60.0
HOST = "app.example.com"

HAPROXY_CONFIG = """\
global
    stats socket /data/admin.sock mode 600 level admin expose-fd listeners

defaults
    mode http
    timeout connect 5s
    timeout client 30s
    timeout server 30s

frontend web
    bind :8443 ssl default-crt /data/certs/default.pem crt /data/certs/
    http-request return status 200 content-type text/plain string ok
"""


def pinned_image(arg: str) -> str:
    """Return an image reference pinned in the Dockerfile, so tests match the product."""
    match = re.search(rf"^ARG {arg}=(\S+)$", (REPO_ROOT / "image/Dockerfile").read_text(), re.M)
    assert match is not None, f"image/Dockerfile no longer sets {arg}"
    return match[1]


# ---- fake Cloudflare: the API calls lego makes, and DNS answers for the zones ----


def _dns_name(data: bytes, offset: int) -> tuple[str, int]:
    labels = []
    while data[offset]:
        length = data[offset]
        labels.append(data[offset + 1 : offset + 1 + length].decode().lower())
        offset += 1 + length
    return ".".join(labels), offset + 1


def _wire_name(name: str) -> bytes:
    return b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\0"


@dataclass
class FakeCloudflare:
    """Zones by name, the TXT records lego creates, and whether tokens may read zones."""

    tokens: set[str]
    zones: dict[str, str] = field(
        default_factory=lambda: {"example.com": "zone-com", "example.net": "zone-net"}
    )
    zone_read: bool = True
    txt: dict[str, tuple[str, str]] = field(default_factory=dict)
    dns_port: int = 0
    api_url: str = ""

    def answer(self, query: bytes) -> bytes:
        """Answer one DNS query: SOA at a zone apex, the TXT records, and nothing else."""
        name, end = _dns_name(query, 12)
        qtype = struct.unpack("!H", query[end : end + 2])[0]
        records: list[tuple[int, bytes]] = []
        if qtype == 6 and name in self.zones:
            soa = _wire_name(f"ns.{name}") + _wire_name(f"admin.{name}")
            records.append((6, soa + struct.pack("!5I", 1, 60, 60, 60, 60)))
        if qtype == 16:
            values = [v.encode() for n, v in list(self.txt.values()) if n == name]
            records += [(16, bytes([len(v)]) + v) for v in values]
        answers = b"".join(
            struct.pack("!HHHIH", 0xC00C, kind, 1, 0, len(data)) + data for kind, data in records
        )
        header = struct.pack("!HHHHHH", query[0] << 8 | query[1], 0x8400, 1, len(records), 0, 0)
        return header + query[12 : end + 4] + answers

    def api(self, method: str, path: str, token: str, body: dict[str, Any]) -> tuple[int, Any]:
        """Answer one API call with a status and its `result`."""
        if token not in self.tokens:
            return 401, None
        if method == "GET" and path.startswith("/zones?"):
            name = re.search(r"name=([^&]+)", path)
            found = name is not None and name[1] in self.zones and self.zone_read
            return 200, [{"id": self.zones[name[1]], "name": name[1]}] if found and name else []
        if method == "GET":
            return 200, []
        record_id = path.rsplit("/", 1)[-1]
        if method == "POST":
            record_id = uuid.uuid4().hex
            self.txt[record_id] = (body["name"].rstrip(".").lower(), body["content"].strip('"'))
            return 200, {"id": record_id, **body}
        self.txt.pop(record_id, None)
        return 200, {"id": record_id}


def _api_handler(fake: FakeCloudflare) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _serve(self) -> None:
            size = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(size)) if size else {}
            token = self.headers.get("Authorization", "").removeprefix("Bearer ")
            status, result = fake.api(self.command, self.path, token, body)
            errors = [] if status == 200 else [{"code": 1000, "message": "Invalid API Token"}]
            raw = json.dumps({"success": status == 200, "errors": errors, "result": result})
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw.encode())

        do_GET = do_POST = do_DELETE = _serve

        def log_message(self, format: str, *args: Any) -> None:
            pass

    return Handler


@pytest.fixture
def cloudflare() -> Iterator[FakeCloudflare]:
    """The fake API and DNS server, on random loopback ports."""
    secrets = (EXAMPLES / "secrets" / "secrets.yaml").read_text()
    fake = FakeCloudflare(tokens=set(re.findall(r'api_token: "([^"]+)"', secrets)))
    dns = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    dns.bind(("127.0.0.1", 0))
    fake.dns_port = dns.getsockname()[1]

    def serve_dns() -> None:
        while True:
            try:
                query, peer = dns.recvfrom(4096)
            except OSError:
                return
            dns.sendto(fake.answer(query), peer)

    api = ThreadingHTTPServer(("127.0.0.1", 0), _api_handler(fake))
    fake.api_url = f"http://127.0.0.1:{api.server_port}"
    threading.Thread(target=serve_dns, daemon=True).start()
    threading.Thread(target=api.serve_forever, daemon=True).start()
    yield fake
    api.shutdown()
    dns.close()


# ---- lego, Pebble and HAProxy ----


@pytest.fixture(scope="module")
def lego_binary() -> Iterator[Path]:
    """Copy the static lego binary out of the pinned image."""
    directory = Path(tempfile.mkdtemp(prefix="hp-lego-"))
    container = f"homeproxy-test-lego-{uuid.uuid4().hex[:12]}"
    docker("create", "--name", container, pinned_image("LEGO_IMAGE"))
    try:
        docker("cp", f"{container}:/lego", str(directory / "lego"))
    finally:
        docker("rm", container)
    try:
        yield directory / "lego"
    finally:
        shutil.rmtree(directory)


@dataclass(frozen=True)
class Pebble:
    """A running Pebble CA: its directory URL and the CA file that signs its HTTPS."""

    url: str
    ca_file: Path


@pytest.fixture(scope="module")
def pebble() -> Iterator[Pebble]:
    """Pebble with 90-day certificates, so a second run finds nothing due."""
    directory = Path(tempfile.mkdtemp(prefix="hp-pebble-"))
    profile = {"description": "90 days", "validityPeriod": NINETY_DAYS}
    config = {
        "pebble": {
            "listenAddress": "0.0.0.0:14000",
            "managementListenAddress": "0.0.0.0:15000",
            "certificate": "test/certs/localhost/cert.pem",
            "privateKey": "test/certs/localhost/key.pem",
            "httpPort": 5002,
            "tlsPort": 5001,
            "ocspResponderURL": "",
            "externalAccountBindingRequired": False,
            "retryAfter": {"authz": 1, "order": 1},
            "keyAlgorithm": "ecdsa",
            "profiles": {"default": profile, "shortlived": profile},
        }
    }
    (directory / "pebble.json").write_text(json.dumps(config))
    directory.chmod(0o755)
    (directory / "pebble.json").chmod(0o644)
    container = f"homeproxy-test-pebble-{uuid.uuid4().hex[:12]}"
    try:
        docker(
            "run", "--detach", "--rm", "--name", container,
            "--env", "PEBBLE_VA_ALWAYS_VALID=1", "--env", "PEBBLE_VA_NOSLEEP=1",
            # Pebble rejects 5% of nonces by default, and lego retries only twice: a flaky test.
            "--env", "PEBBLE_WFE_NONCEREJECT=0",
            "--volume", f"{directory}:/config:ro", "--publish", "127.0.0.1::14000",
            PEBBLE_IMAGE, "-config", "/config/pebble.json",
        )  # fmt: skip
        port = docker("port", container, "14000/tcp").rsplit(":", 1)[-1]
        docker("cp", f"{container}:/test/certs/pebble.minica.pem", str(directory / "ca.pem"))
        instance = Pebble(f"https://127.0.0.1:{port}/dir", directory / "ca.pem")
        context = ssl.create_default_context(cafile=str(instance.ca_file))

        def answers() -> bool:
            with socket.create_connection(("127.0.0.1", int(port)), timeout=2.0) as raw:
                context.wrap_socket(raw, server_hostname="127.0.0.1").close()
            return True

        wait_until(answers, "Pebble to start", timeout=TIMEOUT, not_yet=(OSError,))
        yield instance
    finally:
        subprocess.run(["docker", "rm", "--force", container], capture_output=True, check=False)
        shutil.rmtree(directory)


@dataclass
class Live:
    """A data directory, HAProxy serving its certificates, and the service under test."""

    data: Path
    client: HAProxyClient
    https_port: int
    service: CertificateService

    def served(self, host: str = HOST) -> x509.Certificate:
        """Return the certificate HAProxy presents for an SNI name."""
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        with (
            socket.create_connection(("127.0.0.1", self.https_port), timeout=5.0) as raw,
            context.wrap_socket(raw, server_hostname=host) as tls,
        ):
            der = tls.getpeercert(binary_form=True)
        assert der is not None
        return x509.load_der_x509_certificate(der)

    def pid(self) -> int:
        """Return the pid of the current HAProxy worker."""
        return self.client.show_info().pid


@pytest.fixture
def live(
    lego_binary: Path,
    pebble: Pebble,
    cloudflare: FakeCloudflare,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Live]:
    """HAProxy with only the default certificate, and the example config and secrets."""
    # A short path: Unix socket paths are limited to about 100 bytes.
    data = Path(tempfile.mkdtemp(prefix="hp-"))
    for source, target in (("config/config.yaml", "config"), ("secrets/secrets.yaml", "secrets")):
        (data / target).mkdir()
        shutil.copy(EXAMPLES / source, data / target)
    (data / "haproxy.cfg").write_text(HAPROXY_CONFIG)
    ensure_default_certificate(data / "certs")
    # The token check runs in this process. lego gets nothing from it, only `settings`.
    monkeypatch.setenv("CLOUDFLARE_BASE_URL", cloudflare.api_url)
    client = HAProxyClient(str(data / "master.sock"), str(data / "admin.sock"), reload_timeout=60)
    lego = Lego(
        acme_dir=data / "acme",
        server=pebble.url,
        resolvers=(f"127.0.0.1:{cloudflare.dns_port}",),
        binary=str(lego_binary),
        secrets_dir=data / "secrets",
        settings={
            "LEGO_CA_CERTIFICATES": str(pebble.ca_file),
            "CLOUDFLARE_BASE_URL": cloudflare.api_url,
            "CLOUDFLARE_POLLING_INTERVAL": "1",
            "LEGO_DNS_PROPAGATION_DISABLE_ANS": "true",
        },
    )
    service = CertificateService(Store(data), client, lego, data / "certs")
    container = f"homeproxy-test-certs-{uuid.uuid4().hex[:12]}"
    try:
        docker(
            "run", "--detach", "--rm", "--name", container,
            "--user", f"{os.getuid()}:{os.getgid()}",
            "--volume", f"{data}:/data", "--publish", "127.0.0.1::8443",
            pinned_image("HAPROXY_IMAGE"),
            "haproxy", "-W", "-S", "/data/master.sock,mode,600,level,admin",
            "-f", "/data/haproxy.cfg",
        )  # fmt: skip
        port = int(docker("port", container, "8443/tcp").rsplit(":", 1)[-1])
        instance = Live(data, client, port, service)
        wait_until(
            lambda: instance.served() is not None,
            "HAProxy to start",
            timeout=TIMEOUT,
            not_yet=(OSError,),
        )
        yield instance
    finally:
        subprocess.run(["docker", "rm", "--force", container], capture_output=True, check=False)
        shutil.rmtree(data, ignore_errors=True)


def dns_names(certificate: x509.Certificate) -> set[str]:
    """Return the DNS names of a certificate. The CA chooses their order."""
    extension = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    return set(extension.value.get_values_for_type(x509.DNSName))


# ---- tests ----


def test_issuance_job_reloads_haproxy_to_serve_the_new_wildcard(live: Live) -> None:
    worker = live.pid()
    runner = JobRunner(max_workers=1)
    runner.start()
    try:
        job = submit_issue(runner, live.service, "example.com")
        wait_until(
            lambda: runner.tracker.get(job.id).state.is_final,
            "the issuance job",
            timeout=TIMEOUT,
            not_yet=(OSError,),
        )
        assert runner.tracker.get(job.id).state is JobState.FINISHED
    finally:
        runner.shutdown(wait=True)

    served = live.served()
    assert dns_names(served) == {"example.com", "*.example.com"}
    assert "Pebble" in served.issuer.rfc4514_string()
    # A certificate that HAProxy has not loaded yet needs a reload.
    assert live.pid() != worker
    [info] = live.service.certificates()
    assert (info.name, set(info.names), info.state) == (
        "example.com",
        {"example.com", "*.example.com"},
        "ok",
    )
    assert "Pebble" in info.issuer


@dataclass
class Traffic:
    """Clients that keep requesting while a test runs: new connections and one kept open."""

    live: Live
    stop: threading.Event = field(default_factory=threading.Event)
    served: list[int] = field(default_factory=lambda: [0, 0])
    errors: list[str] = field(default_factory=list)

    def _context(self) -> ssl.SSLContext:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context

    def _request(self, tls: ssl.SSLSocket, keep_alive: bool) -> None:
        connection = "keep-alive" if keep_alive else "close"
        tls.sendall(f"GET / HTTP/1.1\r\nHost: {HOST}\r\nConnection: {connection}\r\n\r\n".encode())
        answer = tls.recv(4096)
        if not answer.startswith(b"HTTP/1.1 200"):
            raise OSError(f"unexpected answer {answer[:40]!r}")

    def _connect(self) -> ssl.SSLSocket:
        raw = socket.create_connection(("127.0.0.1", self.live.https_port), timeout=5.0)
        return self._context().wrap_socket(raw, server_hostname=HOST)

    def new_connections(self) -> None:
        """Open a new TLS connection for every request."""
        while not self.stop.is_set():
            try:
                with self._connect() as tls:
                    self._request(tls, keep_alive=False)
                self.served[0] += 1
            except OSError as error:
                self.errors.append(f"new connection: {error}")

    def kept_connection(self) -> None:
        """Send every request on one connection opened before the renewal."""
        try:
            with self._connect() as tls:
                while not self.stop.is_set():
                    self._request(tls, keep_alive=True)
                    self.served[1] += 1
                    time.sleep(0.01)
        except OSError as error:
            self.errors.append(f"kept connection: {error}")

    def during(self, action: Callable[[], object]) -> object:
        """Run `action` while both clients are busy, and return its result."""
        threads = [threading.Thread(target=self.new_connections)]
        threads.append(threading.Thread(target=self.kept_connection))
        for thread in threads:
            thread.start()
        wait_until(
            lambda: min(self.served) > 5, "traffic to flow", timeout=TIMEOUT, not_yet=(OSError,)
        )
        before = list(self.served)
        try:
            result = action()
            wait_until(lambda: all(a > b + 5 for a, b in zip(self.served, before, strict=True)),
                       "traffic after the action", timeout=TIMEOUT, not_yet=(OSError,))  # fmt: skip
        finally:
            self.stop.set()
            for thread in threads:
                thread.join(TIMEOUT)
        return result


def test_forced_renewal_hot_swaps_with_no_dropped_connection(live: Live) -> None:
    live.service.renew("example.com")
    old = live.served()
    worker = live.pid()
    traffic = Traffic(live)

    delivery = traffic.during(lambda: live.service.renew("example.com", force=True))

    assert delivery is Delivery.HOT_SWAPPED
    assert traffic.errors == []
    assert live.pid() == worker
    new = live.served()
    assert new.serial_number != old.serial_number
    assert dns_names(new) == {"example.com", "*.example.com"}


def test_hot_swap_aborts_a_transaction_left_open_by_a_crash(live: Live) -> None:
    live.service.renew("example.com")
    old = live.served()
    worker = live.pid()
    default = (live.data / "certs" / "default.pem").read_text()
    live.client.set_ssl_cert("/data/certs/default.pem", default)

    delivery = live.service.renew("example.com", force=True)

    assert delivery is Delivery.HOT_SWAPPED
    assert live.pid() == worker
    assert live.served().serial_number != old.serial_number


def test_daily_check_issues_what_is_missing_and_leaves_what_is_not_due(live: Live) -> None:
    live.service.renew("example.com")
    bundle = (live.data / "certs" / "example.com.pem").read_bytes()
    served = live.served().serial_number

    live.service.renew_all()

    assert (live.data / "certs" / "example.com.pem").read_bytes() == bundle
    assert live.service.renew("example.com") is Delivery.UNCHANGED
    assert live.served().serial_number == served
    assert dns_names(live.served("app.example.net")) == {"example.net", "*.example.net"}


def test_daily_check_goes_on_past_a_failed_certificate(
    live: Live, cloudflare: FakeCloudflare
) -> None:
    cloudflare.tokens.discard("fake-cloudflare-token-one")

    with pytest.raises(CertificateError, match=r"^example\.com: ") as failure:
        live.service.renew_all()

    assert "example.net" not in str(failure.value)
    assert dns_names(live.served("app.example.net")) == {"example.net", "*.example.net"}


def test_token_without_zone_read_fails_with_a_clear_error(
    live: Live, cloudflare: FakeCloudflare
) -> None:
    cloudflare.zone_read = False

    with pytest.raises(TokenError, match=r"cannot read the zone example\.com") as failure:
        live.service.renew("example.com")

    assert "Zone:Zone:Read" in str(failure.value)
    assert "fake-cloudflare-token" not in str(failure.value)
    assert not (live.data / "certs" / "example.com.pem").exists()


# ---- other lego DNS providers ----


@dataclass
class HttpReq:
    """lego's httpreq API: its URL and the paths lego called."""

    url: str = ""
    calls: list[str] = field(default_factory=list)


@pytest.fixture
def httpreq(cloudflare: FakeCloudflare) -> Iterator[HttpReq]:
    """The httpreq API on a random loopback port. It puts TXT records into the fake DNS."""
    fake = HttpReq()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            name, value = body["fqdn"].rstrip(".").lower(), body["value"]
            fake.calls.append(self.path)
            if self.path == "/present":
                cloudflare.txt[f"{name} {value}"] = (name, value)
            else:
                cloudflare.txt.pop(f"{name} {value}", None)
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    fake.url = f"http://127.0.0.1:{server.server_port}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield fake
    server.shutdown()


def test_a_domain_on_another_lego_provider_gets_its_certificate(
    live: Live, httpreq: HttpReq, cloudflare: FakeCloudflare
) -> None:
    """example.net moves to lego's httpreq provider, with only its own variable."""
    config = load_tree(live.data / "config" / "config.yaml")
    config["domains"][1].update({"dns_provider": "httpreq", "credentials": "hr"})
    write_tree(live.data / "config" / "config.yaml", config)
    secrets = load_tree(live.data / "secrets" / "secrets.yaml")
    credential = {"provider": "httpreq", "env": {"HTTPREQ_ENDPOINT": httpreq.url}}
    secrets["dns_credentials"]["hr"] = credential
    write_tree(live.data / "secrets" / "secrets.yaml", secrets)

    live.service.renew("example.net")

    served = live.served("app.example.net")
    assert dns_names(served) == {"example.net", "*.example.net"}
    assert "Pebble" in served.issuer.rfc4514_string()
    assert httpreq.calls.count("/present") == 2  # the apex and the wildcard
    assert cloudflare.txt == {}


def test_the_provider_catalog_matches_the_pinned_lego(lego_binary: Path) -> None:
    """Fails after a lego bump until `mise run lego-providers` regenerates the catalog. A bump
    that removes a provider or a variable needs a migration of stored domains first."""
    fresh = read_catalog(str(lego_binary))
    checked_in = TypeAdapter(list[LegoProvider]).validate_json(CATALOG_FILE.read_bytes())

    assert removed(checked_in, fresh) == [], "lego removed these; migrate before the bump"
    assert fresh == checked_in
