"""A HomeProxy container of the built image, run by a test on its own, and its published API.

Setup changes the config, which would disturb the shared compose stack, so these containers are
apart from it. Docker publishes the API on a random loopback port, so parallel checkouts never
clash."""

import ipaddress
import os
import re
import ssl
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import httpx2
import pytest

from tests.integration.haproxy_live import docker

PORT = 8443
TIMEOUT = 90.0
PASSWORD = "first live password"  # noqa: S105


@dataclass
class Server:
    """The container and its published API."""

    name: str
    url: str
    ip: str
    verify: ssl.SSLContext | bool = True

    def logs(self) -> str:
        """The container log."""
        result = subprocess.run(
            ["docker", "logs", self.name], capture_output=True, text=True, check=False
        )
        return result.stdout + result.stderr

    def call(self, method: str, path: str, cookie: str = "", **kwargs: Any) -> httpx2.Response:
        """Send one request with the dashboard's Origin and an optional session cookie."""
        headers = {"Origin": self.url, **({"Cookie": cookie} if cookie else {})}
        return httpx2.request(
            method, f"{self.url}{path}", headers=headers, verify=self.verify, **kwargs
        )

    def wait_for_start(self, count: int) -> None:
        """Wait until the API has started `count` times and answers healthz.

        The first start creates the certificate, which is then the only one trusted.
        """
        deadline = time.monotonic() + TIMEOUT
        while time.monotonic() < deadline:
            if self.logs().count("dashboard listening on") >= count:
                if self.verify is True:
                    self.trust_certificate()
                try:
                    if self.call("GET", "/api/v1/healthz").status_code == 200:
                        return
                except httpx2.TransportError:
                    pass
            time.sleep(1.0)
        pytest.fail(f"the API did not start a {count}. time\n{self.logs()[-4000:]}")

    def trust_certificate(self) -> None:
        """Trust only the dashboard certificate, read from the container without its key."""
        pem = docker(
            "exec", self.name, "sed", "-n", "/BEGIN CERTIFICATE/,$p", "/data/tls/dashboard.pem"
        )
        self.verify = ssl.create_default_context(cadata=pem)


@contextmanager
def start_server(name: str, *env: str) -> Iterator[Server]:
    """Start a container named `name` in setup mode, with `env` as extra `NAME=value` settings,
    and remove it with its volume at the end."""
    project = os.environ.get("COMPOSE_PROJECT_NAME")
    if not project:
        pytest.exit("COMPOSE_PROJECT_NAME is not set. Run `mise run test-integration`.", 2)
    image = f"{os.environ.get('IMAGE_NAME', 'homeproxy')}:{project}"
    subprocess.run(["docker", "rm", "-f", "-v", name], capture_output=True, check=False)
    # Data sources point nowhere, so no job, such as setup's GeoIP download, calls the internet.
    settings = [
        f"HOMEPROXY_BOOTSTRAP_LISTEN=0.0.0.0:{PORT}",
        "HOMEPROXY_CF_RANGES_URL=http://127.0.0.1:9",
        "HOMEPROXY_RELEASES_URL=http://127.0.0.1:9/",
        "HOMEPROXY_GEOIP_URL=http://127.0.0.1:9",
        "HOMEPROXY_UPDATE_URL=http://127.0.0.1:9",
        *env,
    ]
    try:
        docker(
            "run",
            "-d",
            "--name",
            name,
            "-p",
            f"127.0.0.1::{PORT}",
            *(arg for setting in settings for arg in ("-e", setting)),
            image,
        )
        published = docker("port", name, str(PORT)).strip().splitlines()[-1]
        ip = docker(
            "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", name
        )
        running = Server(name=name, url=f"https://{published}", ip=ip.strip())
        running.wait_for_start(1)
        yield running
    except BaseException:
        print(Server(name=name, url="", ip="").logs()[-6000:])
        raise
    finally:
        subprocess.run(["docker", "rm", "-f", "-v", name], capture_output=True, check=False)


def check_detected_values(server: Server) -> None:
    """The setup status reports the container's own subnet and address on the bridge network.
    The placeholder page holds port 80, and the status does not count it as taken."""
    deadline = time.monotonic() + TIMEOUT
    while not placeholder_answers(server.ip):
        if time.monotonic() > deadline:
            pytest.fail("the placeholder page never answered")
        time.sleep(0.5)
    prefix = docker(
        "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPPrefixLen}}{{end}}", server.name
    )
    subnet = ipaddress.ip_interface(f"{server.ip}/{prefix.strip()}").network
    assert server.call("GET", "/api/v1/setup/status").json() == {
        "setup_required": True,
        "detected": {
            "network": {"lan_cidrs": [str(subnet)], "haproxy_lan_ip": server.ip},
            "dns_server": {"listen": f"{server.ip}:5335"},
        },
        "needs": [],
    }


def placeholder_answers(ip: str) -> bool:
    """Whether HAProxy's placeholder page answers on port 80."""
    try:
        return httpx2.get(f"http://{ip}/").status_code == 200
    except httpx2.TransportError:
        return False


def finish_setup(server: Server) -> None:
    """Run the wizard's setup with only the token and `PASSWORD`. The API keeps answering on
    the same address, with no restart.

    Docker's port publishing and the host connect from the bridge gateway, which is in the
    container's subnet, so the detected LAN range lets the tests in.
    """
    token = re.search(r"enter this setup token:\s+(\S+)", server.logs())
    assert token is not None
    check_detected_values(server)
    setup = {"token": token.group(1), "password": PASSWORD}
    # Setup runs the first apply, which can pass httpx's 5 s default under load.
    response = server.call("POST", "/api/v1/setup", json=setup, timeout=TIMEOUT)
    assert response.status_code == 204, response.text
    assert server.call("GET", "/api/v1/setup/status").json() == {"setup_required": False}


def session_cookie(response: httpx2.Response) -> str:
    """The `name=value` part of the session cookie a response sets."""
    return response.headers["set-cookie"].split(";", 1)[0]
