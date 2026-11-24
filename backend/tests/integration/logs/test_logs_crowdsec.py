"""HAProxy in the built image sends its access lines to the collector and to a real CrowdSec.

The CrowdSec engine runs with the `crowdsecurity/haproxy` collection and a syslog acquisition, as in
the dev stack. Its container installs the collection from the CrowdSec hub at start, so this test
needs internet access. Every container and the network get names unique to this run, and ports are
published to random loopback ports, so parallel checkouts cannot clash.
"""

import http.client
import json
import os
import socket
import ssl
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from homeproxy.generator import (
    ensure_default_certificate,
    render,
    write_release,
)
from homeproxy.logs import SYSLOG_SOCKET
from homeproxy.models import Config, parse_route
from tests.conftest import wait_until
from tests.golden.live_proxy import CollectorThread
from tests.integration.crowdsec_lab import CROWDSEC_IMAGE
from tests.integration.haproxy_live import docker

CROWDSEC_SYSLOG_PORT = 514
HAPROXY_PARSER = "crowdsecurity/haproxy-logs"
ACQUISITION = f"""source: syslog
listen_addr: 0.0.0.0
listen_port: {CROWDSEC_SYSLOG_PORT}
max_message_len: 4096
labels:
  type: syslog
"""
HTTPS_PORT = 8443
TCP_PORT = 9100
UPSTREAM = "127.0.0.1:9000"
STARTUP_TIMEOUT = 180.0
SETTLE = 3.0

# Each name is unique to this run, so a line in any log belongs to this test.
TEST_LAYERS = f"""
# The upstream does not log, so each request below makes exactly one line.
frontend test_upstream
    no log
    bind {UPSTREAM}
    http-request return status 200 content-type text/plain string ok

# A `tcp` route on its own port.
frontend test_tcp from tcp
    bind :{TCP_PORT}
    tcp-request content set-var(txn.route) str(game)
    default_backend test_plain

backend test_plain from tcp
    server plain {UPSTREAM}
"""


@dataclass(frozen=True)
class Counts:
    """Lines that CrowdSec's HAProxy parser saw and parsed, and lines the collector parsed."""

    parser_hits: int
    parser_parsed: int
    http_lines: int
    tcp_lines: int


@dataclass(frozen=True)
class Setup:
    crowdsec: str
    haproxy: str
    collector: CollectorThread
    ports: dict[int, int]

    def counts(self) -> Counts:
        parser = crowdsec_metrics(self.crowdsec).get("parsers", {}).get(HAPROXY_PARSER, {})
        entries = self.collector.entries()
        return Counts(
            parser_hits=int(parser.get("hits", 0)),
            parser_parsed=int(parser.get("parsed", 0)),
            http_lines=sum(1 for entry in entries if entry.kind == "http"),
            tcp_lines=sum(1 for entry in entries if entry.kind == "tcp"),
        )


def crowdsec_metrics(container: str) -> dict[str, Any]:
    metrics: dict[str, Any] = json.loads(
        docker("exec", container, "cscli", "metrics", "-o", "json")
    )
    return metrics


def https_request(port: int, sni: str, path: str) -> int:
    """Send one request over TLS with this SNI and Host, and return the status code."""
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    connection = http.client.HTTPSConnection("127.0.0.1", port, timeout=10, context=context)
    try:
        # `http.client` sends no SNI to an IP address, so the socket is wrapped here.
        raw = socket.create_connection(("127.0.0.1", port), timeout=10)
        connection.sock = context.wrap_socket(raw, server_hostname=sni)
        connection.request("GET", path, headers={"Host": sni})
        response = connection.getresponse()
        response.read()
        return response.status
    finally:
        connection.close()


def tcp_request(port: int) -> None:
    with socket.create_connection(("127.0.0.1", port), timeout=10) as client:
        client.sendall(b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        while client.recv(4096):
            pass


def write_release_dir(data_dir: Path, crowdsec_address: str) -> None:
    config = Config.model_validate(
        {
            "version": 1,
            "dashboard": {
                "listen": "192.168.1.10:8443",
                "password_hash": "$argon2id$v=19$m=65536,t=3,p=4$ZmFrZS1zYWx0$ZmFrZS1oYXNo",
            },
            "network": {
                "lan_cidrs": ["192.168.1.0/24"],
                "haproxy_lan_ip": "192.168.1.10",
                "http_port": 8080,
                "https_port": HTTPS_PORT,
            },
            "dns_server": {"listen": "192.168.1.10:5335"},
            "crowdsec": {
                "enabled": True,
                "lapi_url": "http://127.0.0.1:18080",
                "syslog": f"{crowdsec_address}:{CROWDSEC_SYSLOG_PORT}",
            },
        }
    )
    route = parse_route(
        {
            "version": 1,
            "name": "app",
            "type": "http",
            "hosts": ["app.example.com"],
            "exposure": "public",
            "upstreams": [{"address": UPSTREAM}],
        }
    )
    for name in ("releases", "certs", "state"):
        (data_dir / name).mkdir()
    ensure_default_certificate(data_dir / "certs")
    release = render(config, [route], includes={"test-layers": TEST_LAYERS})
    write_release(release, data_dir / "releases" / "first")
    (data_dir / "generated").symlink_to("releases/first")


@pytest.fixture
def setup(tmp_path: Path) -> Iterator[Setup]:
    project = os.environ.get("COMPOSE_PROJECT_NAME")
    if not project:
        pytest.exit("COMPOSE_PROJECT_NAME is not set. Run `mise run test-integration`.", 2)
    image = f"{os.environ.get('IMAGE_NAME', 'homeproxy')}:{project}"
    prefix = f"{project}-test-logs-{uuid.uuid4().hex[:8]}"
    network, crowdsec, haproxy = f"{prefix}-net", f"{prefix}-crowdsec", f"{prefix}-haproxy"
    acquisition = tmp_path / "acquis.yaml"
    acquisition.write_text(ACQUISITION)
    acquisition.chmod(0o644)
    data_dir, run_dir = tmp_path / "data", tmp_path / "run"
    data_dir.mkdir()
    run_dir.mkdir()
    docker("network", "create", network)
    try:
        docker(
            "run",
            "--detach",
            f"--name={crowdsec}",
            f"--network={network}",
            "--env=COLLECTIONS=crowdsecurity/haproxy",
            "--env=DISABLE_ONLINE_API=true",
            # The database is thrown away with the container.
            "--env=CROWDSEC_BYPASS_DB_VOLUME_CHECK=true",
            f"--volume={acquisition}:/etc/crowdsec/acquis.d/haproxy.yaml:ro",
            CROWDSEC_IMAGE,
        )
        address = docker(
            "inspect",
            "--format",
            f'{{{{(index .NetworkSettings.Networks "{network}").IPAddress}}}}',
            crowdsec,
        )
        write_release_dir(data_dir, address)
        with CollectorThread(run_dir / Path(SYSLOG_SOCKET).name) as collector:
            docker(
                "run",
                "--detach",
                f"--name={haproxy}",
                f"--network={network}",
                f"--user={os.getuid()}:{os.getgid()}",
                f"--tmpfs=/run/haproxy:uid={os.getuid()}",
                f"--volume={data_dir}:/data",
                f"--volume={run_dir}:{Path(SYSLOG_SOCKET).parent}",
                f"--publish=127.0.0.1::{HTTPS_PORT}",
                f"--publish=127.0.0.1::{TCP_PORT}",
                "--entrypoint=haproxy",
                image,
                "-W",
                "-db",
                "-f",
                "/data/generated/conf.d/",
            )
            ports = {
                port: int(docker("port", haproxy, f"{port}/tcp").splitlines()[0].rpartition(":")[2])
                for port in (HTTPS_PORT, TCP_PORT)
            }
            yield Setup(crowdsec, haproxy, collector, ports)
    except BaseException:
        for container in (haproxy, crowdsec):
            print(f"--- {container}\n{docker('logs', '--tail', '60', container, check=False)}")
        raise
    finally:
        docker("rm", "--force", haproxy, crowdsec, check=False)
        docker("network", "rm", network, check=False)


def test_crowdsec_parses_access_lines(setup: Setup) -> None:
    """That each connection is logged once, behind the SNI layer too, is in
    `tests/golden/test_golden_logs.py`."""
    https = setup.ports[HTTPS_PORT]

    # CrowdSec installs its collection first. Lines it receives before it listens are lost.
    # HAProxy may not listen yet either: Docker's port forward then resets or closes the
    # connection, which shows as a connection or TLS error.
    def crowdsec_receives() -> bool:
        assert https_request(https, "app.example.com", "/warm-up") == 200
        return setup.counts().parser_hits > 0

    wait_until(
        crowdsec_receives,
        "CrowdSec to read HAProxy's lines",
        timeout=STARTUP_TIMEOUT,
        interval=1.0,
        not_yet=(ConnectionError, ssl.SSLError),
    )
    # A warm-up line still on its way would count below. Some were lost, so no count says when
    # the last one is in.
    time.sleep(SETTLE)
    before = setup.counts()

    http_lines, tcp_lines = 3, 2
    for index in range(http_lines):
        assert https_request(https, "app.example.com", f"/direct/{index}") == 200
    for _ in range(tcp_lines):
        tcp_request(setup.ports[TCP_PORT])

    def all_lines_arrived() -> bool:
        counts = setup.counts()
        return counts.parser_hits - before.parser_hits >= http_lines + tcp_lines

    wait_until(all_lines_arrived, "CrowdSec to read every line", timeout=30, interval=1.0)
    after = setup.counts()

    assert after.parser_hits - before.parser_hits == http_lines + tcp_lines
    # Every HTTP line ends with the appended fields, and the parser still parses it. CrowdSec
    # detection covers HTTP routes only: TCP lines reach the parser, which has no pattern for them.
    assert after.parser_parsed - before.parser_parsed >= http_lines
    assert after.http_lines - before.http_lines == http_lines
    assert after.tcp_lines - before.tcp_lines == tcp_lines
    assert {entry.route for entry in setup.collector.entries() if entry.kind == "tcp"} == {"game"}
