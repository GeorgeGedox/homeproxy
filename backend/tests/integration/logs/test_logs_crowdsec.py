"""HAProxy in the built image sends its access lines to the collector and to a real CrowdSec.

The CrowdSec engine runs with the `crowdsecurity/haproxy` collection and a syslog acquisition, as in
the dev stack. Its container installs the collection from the CrowdSec hub at start, so this test
needs internet access. Every container and the network get names unique to this run, and ports are
published to random loopback ports, so parallel checkouts cannot clash.
"""

import http.client
import ipaddress
import json
import os
import random
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
from homeproxy.store import load_tree
from tests.conftest import wait_until
from tests.golden.live_proxy import CollectorThread
from tests.integration.crowdsec_lab import CROWDSEC_IMAGE, REPO_ROOT
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
SCENARIO = "homeproxy/unknown-host"
SCENARIO_CONFIG = "crowdsec_unknown_host"
SCENARIO_TARGET = "/etc/crowdsec/scenarios/homeproxy-unknown-host.yaml"
TEST_NET = ipaddress.IPv4Network("198.51.100.0/24")
"""TEST-NET-2. CrowdSec's default whitelist drops private sources, and this range is not private."""
BAN_TIMEOUT = 60.0

# A client request from its own address. It sends the SNI, which may be empty, and the Host header
# of each "<sni>|<host>|<path>" argument over a new TLS connection, and prints each status.
CLIENT = """
import http.client, socket, ssl, sys
context = ssl.create_default_context()
context.check_hostname = False
context.verify_mode = ssl.CERT_NONE
address, port = sys.argv[1], int(sys.argv[2])
for request in sys.argv[3:]:
    sni, host, path = request.split("|")
    connection = http.client.HTTPSConnection(address, port, timeout=10, context=context)
    raw = socket.create_connection((address, port), timeout=10)
    connection.sock = context.wrap_socket(raw, server_hostname=sni or None)
    connection.request("GET", path, headers={"Host": host})
    print(connection.getresponse().status)
"""

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
    network: str
    subnet: ipaddress.IPv4Network
    haproxy_address: str
    image: str

    def counts(self) -> Counts:
        parser = crowdsec_metrics(self.crowdsec).get("parsers", {}).get(HAPROXY_PARSER, {})
        entries = self.collector.entries()
        return Counts(
            parser_hits=int(parser.get("hits", 0)),
            parser_parsed=int(parser.get("parsed", 0)),
            http_lines=sum(1 for entry in entries if entry.kind == "http"),
            tcp_lines=sum(1 for entry in entries if entry.kind == "tcp"),
        )


def scenario_text() -> str:
    """Return the scenario that `deploy/compose.crowdsec.yaml` installs, after a check that the dev
    stack and `docs/crowdsec.md` install the same one."""
    deploy = REPO_ROOT / "deploy"
    text = str(load_tree(deploy / "compose.crowdsec.yaml")["configs"][SCENARIO_CONFIG]["content"])
    dev = load_tree(deploy / "compose.dev.yaml")["configs"][SCENARIO_CONFIG]["content"]
    assert str(dev) == text
    # The docs show it in the same `configs` block, six spaces in.
    quoted = "".join(f"      {line}" if line.strip() else line for line in text.splitlines(True))
    assert quoted in (REPO_ROOT / "docs" / "crowdsec.md").read_text()
    return text


def public_network(name: str) -> ipaddress.IPv4Network:
    """Create a Docker network on a free block of `TEST_NET`, so its clients have public addresses.
    Another run may hold a block, so the blocks are tried in random order."""
    blocks = list(TEST_NET.subnets(new_prefix=28))
    random.shuffle(blocks)
    for block in blocks:
        if docker("network", "create", f"--subnet={block}", name, check=False):
            return block
    pytest.fail(f"no free /28 in {TEST_NET} for a Docker network")


def ask(setup: Setup, client: str, *requests: str) -> list[int]:
    """Send `requests`, each "<sni>|<host>|<path>", from a container at `client` to HAProxy."""
    out = docker(
        "run",
        "--rm",
        f"--network={setup.network}",
        f"--ip={client}",
        "--entrypoint=/usr/bin/python3",
        setup.image,
        "-c",
        CLIENT,
        setup.haproxy_address,
        str(HTTPS_PORT),
        *requests,
    )
    return [int(status) for status in out.split()]


def decisions(setup: Setup, address: str) -> list[dict[str, Any]]:
    out = docker("exec", setup.crowdsec, "cscli", "decisions", "list", "-i", address, "-o", "json")
    found: list[dict[str, Any]] | None = json.loads(out)
    return found or []


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


@pytest.fixture(scope="module")
def setup(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Setup]:
    """HAProxy and CrowdSec on a network of public addresses. CrowdSec reads HAProxy's lines."""
    project = os.environ.get("COMPOSE_PROJECT_NAME")
    if not project:
        pytest.exit("COMPOSE_PROJECT_NAME is not set. Run `mise run test-integration`.", 2)
    image = f"{os.environ.get('IMAGE_NAME', 'homeproxy')}:{project}"
    prefix = f"{project}-test-logs-{uuid.uuid4().hex[:8]}"
    network, crowdsec, haproxy = f"{prefix}-net", f"{prefix}-crowdsec", f"{prefix}-haproxy"
    tmp_path = tmp_path_factory.mktemp("crowdsec")
    acquisition = tmp_path / "acquis.yaml"
    acquisition.write_text(ACQUISITION)
    scenario = tmp_path / "scenario.yaml"
    scenario.write_text(scenario_text())
    for mounted in (acquisition, scenario):
        mounted.chmod(0o644)
    data_dir, run_dir = tmp_path / "data", tmp_path / "run"
    data_dir.mkdir()
    run_dir.mkdir()
    subnet = public_network(network)
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
            f"--volume={scenario}:{SCENARIO_TARGET}:ro",
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
            template = f'{{{{(index .NetworkSettings.Networks "{network}").IPAddress}}}}'
            address = docker("inspect", "--format", template, haproxy)
            running = Setup(crowdsec, haproxy, collector, ports, network, subnet, address, image)
            wait_for_crowdsec(running)
            yield running
    except BaseException:
        for container in (haproxy, crowdsec):
            print(f"--- {container}\n{docker('logs', '--tail', '60', container, check=False)}")
        raise
    finally:
        docker("rm", "--force", haproxy, crowdsec, check=False)
        docker("network", "rm", network, check=False)


def wait_for_crowdsec(setup: Setup) -> None:
    """Wait until CrowdSec reads HAProxy's lines.

    CrowdSec installs its collection first. Lines it receives before it listens are lost. HAProxy
    may not listen yet either: Docker's port forward then resets or closes the connection, which
    shows as a connection or TLS error.
    """

    def crowdsec_receives() -> bool:
        assert https_request(setup.ports[HTTPS_PORT], "app.example.com", "/warm-up") == 200
        return setup.counts().parser_hits > 0

    wait_until(
        crowdsec_receives,
        "CrowdSec to read HAProxy's lines",
        timeout=STARTUP_TIMEOUT,
        interval=1.0,
        not_yet=(ConnectionError, ssl.SSLError),
    )


def test_crowdsec_parses_access_lines(setup: Setup) -> None:
    """That each connection is logged once, behind the SNI layer too, is in
    `tests/golden/test_golden_logs.py`."""
    https = setup.ports[HTTPS_PORT]
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


def test_a_client_that_asks_twice_for_an_unknown_host_is_banned(setup: Setup) -> None:
    """The scenario of `docs/crowdsec.md`. One request for an unknown host, such as a mistyped
    address, does not ban, and neither does the favicon a browser asks for after it. A visitor of a
    site never counts."""
    once, browser, visitor, twice = (str(setup.subnet[index]) for index in range(10, 14))
    bare_ip = f"|{setup.haproxy_address}|/"
    before = setup.counts()
    poured_before = scenario_pours(setup)

    assert ask(setup, once, bare_ip) == [404]
    unknown = "unknown.example.net|unknown.example.net"
    assert ask(setup, browser, f"{unknown}|/", f"{unknown}|/favicon.ico") == [404, 404]
    site = "app.example.com|app.example.com"
    assert ask(setup, visitor, f"{site}|/", f"{site}|/other") == [200, 200]
    assert ask(setup, twice, bare_ip, bare_ip) == [404, 404]

    wait_until(lambda: decisions(setup, twice), "a ban of the client", timeout=BAN_TIMEOUT)
    # Every line is in: the scenario took the 4 that count, and the parser read all 7.
    wait_until(
        lambda: (
            scenario_pours(setup) - poured_before >= 4
            and setup.counts().parser_hits - before.parser_hits >= 7
        ),
        "CrowdSec to read every line",
        timeout=BAN_TIMEOUT,
        details=lambda: (
            f"{scenario_pours(setup) - poured_before} pours, "
            f"{setup.counts().parser_hits - before.parser_hits} lines, "
            f"scenarios {crowdsec_metrics(setup.crowdsec).get('scenarios')}"
        ),
    )
    assert [decision["scenario"] for decision in decisions(setup, twice)] == [SCENARIO]
    assert [decisions(setup, client) for client in (once, browser, visitor)] == [[], [], []]


def scenario_pours(setup: Setup) -> int:
    """Return how many lines the scenario has taken."""
    buckets = crowdsec_metrics(setup.crowdsec).get("scenarios", {})
    return int(buckets.get(SCENARIO, {}).get("pour", 0))
