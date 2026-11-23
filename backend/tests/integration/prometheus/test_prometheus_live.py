"""The Prometheus exporter in a real HAProxy that the apply pipeline configured.

Relays in an include forward to the exporter's listener from fixed loopback addresses: one in the
LAN, one outside it. The test reaches the relays on the container's own address, so no host port
is bound.
"""

import http.client
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from homeproxy.haproxy.client import HAProxyClient
from homeproxy.haproxy.pipeline import Pipeline
from homeproxy.store import Store
from tests.integration.haproxy_live import docker, haproxy_image, wait_for_haproxy
from tests.integration.relay_seed import (
    HTTP_PORT,
    HTTPS_PORT,
    LAN,
    OTHER,
    PASSWORD_HASH,
    PLACEHOLDER,
    SEED,
)

EXPORTER = "127.0.0.1:9405"
MISSING = "10.255.255.1:9405"
"""An address the container does not have, as after a change of the host's LAN IP."""
RELAY_PORTS = {LAN: 9106, OTHER: 9107}
RELAYS = "".join(
    f"""
listen relay_{port} from tcp
    bind :{port}
    server exporter {EXPORTER} source {source}
"""
    for source, port in RELAY_PORTS.items()
)

CONFIG = f"""\
version: 1
dashboard:
  listen: 192.168.1.10:9443
  password_hash: "{PASSWORD_HASH}"
network:
  lan_cidrs: [{LAN}/32]
  haproxy_lan_ip: 192.168.1.10
  http_port: {HTTP_PORT}
  https_port: {HTTPS_PORT}
dns_server:
  listen: 192.168.1.10:5335
prometheus:
  enabled: true
  listen: {EXPORTER}
"""

# A sample line of the text exposition format: a metric name, optional labels and a value.
SAMPLE = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*(\{[^}]*\})? \S+")


def seed(data: Path, exporter: str) -> None:
    shutil.copytree(SEED / "secrets", data / "secrets")
    (data / "config" / "routes").mkdir(parents=True)
    (data / "config" / "includes").mkdir()
    (data / "config" / "config.yaml").write_text(CONFIG.replace(EXPORTER, exporter))
    (data / "config" / "includes" / "relays.cfg").write_text(RELAYS)
    placeholder = data / "releases" / "placeholder" / "conf.d"
    placeholder.mkdir(parents=True)
    (placeholder / "00-placeholder.cfg").write_text(PLACEHOLDER)
    (data / "generated").symlink_to("releases/placeholder")


@contextmanager
def haproxy(exporter: str) -> Iterator[tuple[str, str, Pipeline]]:
    """Run HAProxy on the placeholder with the exporter at `exporter` in `config.yaml`. Yield the
    container name, its address and a pipeline for it."""
    # Unix socket paths are limited to about 100 bytes, so the temp path stays short.
    data = Path(tempfile.mkdtemp(prefix="hp-"))
    container = f"homeproxy-test-prometheus-{uuid.uuid4().hex[:12]}"
    run_dir = data / "run"
    run_dir.mkdir()
    seed(data, exporter)
    try:
        docker(
            "run",
            "--detach",
            f"--name={container}",
            f"--user={os.getuid()}:{os.getgid()}",
            f"--volume={data}:/data",
            f"--volume={data}:{data}",
            f"--volume={run_dir}:/run/haproxy",
            haproxy_image(),
            *("haproxy", "-W", "-S", "/run/haproxy/master.sock,mode,600,level,admin"),
            *("-f", "/data/generated/conf.d/"),
        )
        address = docker(
            "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", container
        )
        client = HAProxyClient(str(run_dir / "master.sock"), str(run_dir / "admin.sock"))
        pipeline = Pipeline(
            Store(data),
            client=client,
            haproxy_command=("docker", "exec", container, "haproxy"),
            probe_host=address,
        )
        wait_for_haproxy(container, lambda: client.show_info().pid > 0)
        yield container, address, pipeline
    finally:
        subprocess.run(["docker", "rm", "--force", container], capture_output=True, timeout=60)
        shutil.rmtree(data, ignore_errors=True)


@pytest.fixture(scope="module")
def address() -> Iterator[str]:
    """Run HAProxy after one apply with the exporter on, and return the container's address."""
    with haproxy(EXPORTER) as (_, address, pipeline):
        result = pipeline.apply("admin")
        assert result.success, result
        yield address


def scrape(address: str, source: str, path: str = "/metrics") -> tuple[int, str, str]:
    """Return the status, content type and body of one request from `source`."""
    connection = http.client.HTTPConnection(address, RELAY_PORTS[source], timeout=5)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        body = response.read().decode()
    finally:
        connection.close()
    return response.status, response.getheader("Content-Type", ""), body


def test_lan_client_scrapes_prometheus_metrics(address: str) -> None:
    status, content_type, body = scrape(address, LAN)

    assert status == 200
    assert content_type.startswith("text/plain; version=0.0.4")
    assert "# TYPE haproxy_process_uptime_seconds gauge" in body
    samples = [line for line in body.splitlines() if line and not line.startswith("#")]
    assert samples
    assert [line for line in samples if SAMPLE.fullmatch(line) is None] == []


def test_client_outside_the_lan_is_refused(address: str) -> None:
    assert scrape(address, OTHER)[0] == 403


def test_other_paths_are_not_found(address: str) -> None:
    assert scrape(address, LAN, "/")[0] == 404


def test_exporter_on_an_address_the_host_lacks_does_not_stop_haproxy() -> None:
    """After a change of the host's LAN IP, the saved exporter address is gone. The apply and a
    cold start of HAProxy still succeed, and the proxy ports answer."""
    with haproxy(MISSING) as (container, _, pipeline):
        result = pipeline.apply("admin")
        assert result.success, result
        assert any(warning.startswith("prometheus.listen:") for warning in result.warnings)

        docker("restart", container)

        wait_for_haproxy(container, lambda: pipeline.client.show_info().pid > 0)
        address = docker(
            "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", container
        )
        connection = http.client.HTTPConnection(address, HTTP_PORT, timeout=5)
        try:
            connection.request("GET", "/")
            # The HTTP port sends any named host to HTTPS.
            assert connection.getresponse().status == 301
        finally:
            connection.close()
