"""Auth against the built image, with clients on a LAN and on a non-private network.

This module runs its own HomeProxy container, so it can change config and restart the API
without disturbing the shared compose stack. Two Docker networks give the clients real source
addresses:

- `lan`: a private subnet that Docker picks. It becomes `network.lan_cidrs`.
- `wan`: a /24 in 198.18.0.0/15 (benchmarking space), which is neither private nor in the LAN.

Clients are containers of the same image that run a small Python script with `docker exec`, so no
host port is published and parallel checkouts never clash. Names start with `test_name_prefix`.

The phases run in order: setup mode on the default route's address, setup mode on every address,
then configured on every address, then configured on the LAN IP.
"""

import json
import os
import random
import re
import secrets
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest

from homeproxy.auth.passwords import hash_password
from tests.integration.dashboard_client import CLIENT_SCRIPT, PYTHON
from tests.integration.haproxy_live import docker

PASSWORD = "integration password"  # noqa: S105
PORT = 8443
TIMEOUT = 90.0


@dataclass
class Lab:
    """The server, two clients and their networks."""

    prefix: str
    server: str
    lan_client: str
    wan_client: str
    lan_ip: str
    lan_subnet: str
    wan_ip: str = ""

    def request(self, client: str, host: str, steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Send requests from a client container and return the results."""
        out = docker(
            "exec", client, PYTHON, "-c", CLIENT_SCRIPT, host, str(PORT), json.dumps(steps)
        )
        results: list[dict[str, Any]] = json.loads(out)
        return results

    def get(self, client: str, host: str, path: str) -> dict[str, Any]:
        """One GET request."""
        return self.request(client, host, [{"method": "GET", "path": path}])[0]

    def logs(self) -> str:
        """The server container log."""
        result = subprocess.run(
            ["docker", "logs", self.server], capture_output=True, text=True, check=False
        )
        return result.stdout + result.stderr

    def wait_for_listen(self, count: int, host: str) -> None:
        """Wait until the API has started `count` times and answers healthz from the LAN."""
        deadline = time.monotonic() + TIMEOUT
        while time.monotonic() < deadline:
            if self.logs().count("dashboard listening on") >= count:
                result = self.get(self.lan_client, host, "/api/v1/healthz")
                if result.get("status") == 200:
                    return
            time.sleep(1.0)
        pytest.fail(f"the API did not start a {count}. time on {host}\n{self.logs()[-4000:]}")

    def restart_api(self) -> int:
        """Restart the API service and return the start count to wait for."""
        count = self.logs().count("dashboard listening on") + 1
        docker("exec", self.server, "/command/s6-svc", "-r", "/run/service/api")
        return count


def _network_ip(container: str, network: str) -> str:
    networks = json.loads(docker("inspect", "-f", "{{json .NetworkSettings.Networks}}", container))
    address: str = networks[network]["IPAddress"]
    assert address, f"{container} has no address on {network}"
    return address


def _create_wan_network(name: str) -> None:
    """Create the non-private network on a free /24 of 198.18.0.0/15."""
    last = ""
    for _ in range(20):
        third = random.randrange(256)  # noqa: S311 - not security sensitive
        second = random.choice((18, 19))  # noqa: S311
        subnet = f"198.{second}.{third}.0/24"
        result = subprocess.run(
            ["docker", "network", "create", "--subnet", subnet, name],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        if result.returncode == 0:
            return
        last = result.stderr.strip()
    pytest.fail(f"could not create a 198.18.0.0/15 network: {last}")


@pytest.fixture(scope="module")
def lab(test_name_prefix: str) -> Iterator[Lab]:
    """Start the server and the clients, and remove everything at the end."""
    project = os.environ.get("COMPOSE_PROJECT_NAME")
    if not project:
        pytest.exit("COMPOSE_PROJECT_NAME is not set. Run `mise run test-integration`.", 2)
    # The image tag is the checkout's project name.
    image = f"{os.environ.get('IMAGE_NAME', 'homeproxy')}:{project}"
    prefix = f"{test_name_prefix}-auth"
    lan, wan = f"{prefix}-a-lan", f"{prefix}-b-wan"
    server, lan_client, wan_client = f"{prefix}-server", f"{prefix}-lan", f"{prefix}-wan"

    def cleanup() -> None:
        for container in (server, lan_client, wan_client):
            docker("rm", "-f", "-v", container, check=False)
        for network in (lan, wan):
            docker("network", "rm", network, check=False)

    cleanup()
    running: Lab | None = None
    try:
        docker("network", "create", lan)
        _create_wan_network(wan)
        # Only the LAN network at first, so the default route is on it.
        # The internet is off for the run: these jobs fail at once on a closed port.
        offline = (
            "--env=HOMEPROXY_CF_RANGES_URL=http://127.0.0.1:9",
            "--env=HOMEPROXY_RELEASES_URL=http://127.0.0.1:9/",
            "--env=HOMEPROXY_GEOIP_URL=http://127.0.0.1:9",
            "--env=HOMEPROXY_UPDATE_URL=http://127.0.0.1:9",
        )
        docker("run", "-d", "--name", server, "--network", lan, *offline, image)
        for name, network in ((lan_client, lan), (wan_client, wan)):
            docker(
                "run",
                "-d",
                "--name",
                name,
                "--network",
                network,
                "--entrypoint",
                "sleep",
                image,
                "infinity",
            )
        subnet = json.loads(docker("network", "inspect", "-f", "{{json .IPAM.Config}}", lan))
        running = Lab(
            prefix=prefix,
            server=server,
            lan_client=lan_client,
            wan_client=wan_client,
            lan_ip=_network_ip(server, lan),
            lan_subnet=subnet[0]["Subnet"],
        )
        yield running
    except BaseException:
        if running is not None:
            print(running.logs()[-6000:])
        raise
    finally:
        cleanup()


def _config(listen: str, lan_subnet: str, lan_ip: str) -> str:
    """A minimal config.yaml whose password the test sets with the CLI afterwards."""
    placeholder = hash_password(secrets.token_urlsafe(16))
    return (
        "version: 1\n"
        "dashboard:\n"
        f"  listen: {listen}\n"
        f'  password_hash: "{placeholder}"\n'
        "network:\n"
        f"  lan_cidrs: [{lan_subnet}]\n"
        f"  haproxy_lan_ip: {lan_ip}\n"
        "dns_server:\n"
        f"  listen: {lan_ip}:5335\n"
    )


def _no_http_response(result: dict[str, Any]) -> bool:
    return "status" not in result and "error" in result


def _header(result: dict[str, Any], name: str) -> str:
    return next((value for key, value in result["headers"] if key == name), "")


def test_auth_end_to_end(lab: Lab) -> None:
    """Setup mode, the source filter in both modes, set-password as root, and login over HTTPS
    on the LAN IP with the throttle and the Origin check."""
    # 1. Setup mode on the default route's address, with a token in the log and no password.
    lab.wait_for_listen(1, lab.lan_ip)
    logs = lab.logs()
    assert f"dashboard listening on {lab.lan_ip}:{PORT} (setup)" in logs
    token = re.search(r"enter this setup token:\s+(\S+)", logs)
    assert token is not None and len(token.group(1)) >= 32
    status = lab.get(lab.lan_client, lab.lan_ip, "/api/v1/setup/status")
    assert json.loads(status["body"])["setup_required"] is True
    origin = {"Origin": f"https://{lab.lan_ip}:{PORT}"}
    no_password = lab.request(
        lab.lan_client,
        lab.lan_ip,
        [
            {
                "method": "POST",
                "path": "/api/v1/auth/login",
                "headers": origin,
                "body": {"password": password},
            }
            for password in ("admin", "homeproxy", "password", "")
        ],
    )
    assert [item["status"] for item in no_password] == [409, 409, 409, 422]

    # 2. Setup mode on every address: a non-private source gets no HTTP response.
    docker("network", "connect", f"{lab.prefix}-b-wan", lab.server)
    lab.wan_ip = _network_ip(lab.server, f"{lab.prefix}-b-wan")
    docker(
        "exec",
        lab.server,
        "sh",
        "-c",
        "printf %s 0.0.0.0:8443 > /run/s6/container_environment/HOMEPROXY_BOOTSTRAP_LISTEN",
    )
    lab.wait_for_listen(lab.restart_api(), lab.lan_ip)
    assert f"dashboard listening on 0.0.0.0:{PORT} (setup)" in lab.logs()
    assert _no_http_response(lab.get(lab.wan_client, lab.wan_ip, "/api/v1/healthz"))

    # 3. Configured, listening on every address: LAN in, non-LAN dropped. The password is set
    # with the CLI as root, which must leave the file owned by the service user.
    docker(
        "exec",
        "-i",
        "-u",
        "homeproxy",
        lab.server,
        "sh",
        "-c",
        "cat > /data/config/.new && mv /data/config/.new /data/config/config.yaml",
        stdin=_config(f"0.0.0.0:{PORT}", lab.lan_subnet, lab.lan_ip),
    )
    out = docker(
        "exec",
        "-i",
        lab.server,
        "/opt/homeproxy/venv/bin/homeproxy",
        "set-password",
        "--stdin",
        stdin=PASSWORD + "\n",
    )
    assert "Password changed" in out
    owner = docker("exec", lab.server, "stat", "-c", "%U", "/data/config/config.yaml")
    assert owner.strip() == "homeproxy"
    lab.wait_for_listen(lab.restart_api(), lab.lan_ip)
    assert f"dashboard listening on 0.0.0.0:{PORT} (dashboard.listen)" in lab.logs()
    assert _no_http_response(lab.get(lab.wan_client, lab.wan_ip, "/api/v1/healthz"))
    status = lab.get(lab.lan_client, lab.lan_ip, "/api/v1/setup/status")
    assert json.loads(status["body"]) == {"setup_required": False}

    # 4. Configured on the LAN IP: login over HTTPS, the session, the throttle and Origin.
    docker(
        "exec",
        "-u",
        "homeproxy",
        lab.server,
        "sed",
        "-i",
        f"s/^  listen: 0.0.0.0:{PORT}$/  listen: {lab.lan_ip}:{PORT}/",
        "/data/config/config.yaml",
    )
    lab.wait_for_listen(lab.restart_api(), lab.lan_ip)
    assert f"dashboard listening on {lab.lan_ip}:{PORT} (dashboard.listen)" in lab.logs()
    assert _no_http_response(lab.get(lab.wan_client, lab.wan_ip, "/api/v1/healthz"))

    login = {"method": "POST", "path": "/api/v1/auth/login", "headers": origin}
    first = lab.request(lab.lan_client, lab.lan_ip, [{**login, "body": {"password": PASSWORD}}])
    assert first[0]["status"] == 204
    cookie = _header(first[0], "set-cookie")
    for attribute in ("HttpOnly", "Secure", "SameSite=strict", "Path=/"):
        assert attribute.lower() in cookie.lower()
    session = cookie.split(";", 1)[0]
    me = lab.request(
        lab.lan_client,
        lab.lan_ip,
        [
            {"method": "GET", "path": "/api/v1/auth/me", "headers": {"Cookie": session}},
            {"method": "GET", "path": "/api/v1/auth/me"},
            {"method": "POST", "path": "/api/v1/auth/logout", "headers": {"Cookie": session}},
        ],
    )
    assert [item["status"] for item in me] == [200, 401, 403]

    attempts = lab.request(
        lab.lan_client,
        lab.lan_ip,
        [{**login, "body": {"password": "wrong password"}}] * 5
        + [{**login, "body": {"password": PASSWORD}}],
    )
    assert [item["status"] for item in attempts] == [401, 401, 401, 401, 401, 429]
    assert int(_header(attempts[5], "retry-after")) > 0
    assert PASSWORD not in lab.logs()
