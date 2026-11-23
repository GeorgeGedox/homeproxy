"""Setup, password change and settings against the built image, over verified HTTPS.

This module runs its own HomeProxy container, because setup changes its config, which would
disturb the shared compose stack. Docker publishes the API on a random
loopback port, so parallel checkouts never clash.
"""

import re
import time
from collections.abc import Iterator

import httpx2
import pytest

from tests.integration.haproxy_live import docker
from tests.integration.image_server import (
    PASSWORD,
    PORT,
    TIMEOUT,
    Server,
    finish_setup,
    session_cookie,
    start_server,
)

NEW_PASSWORD = "second live password"  # noqa: S105


@pytest.fixture(scope="module")
def server(test_name_prefix: str) -> Iterator[Server]:
    """A container in setup mode for this module."""
    with start_server(f"{test_name_prefix}-api") as running:
        yield running


def test_setup_then_password_and_settings_through_the_image(server: Server) -> None:
    """The wizard's setup, login on the same server, a password change and a staged settings
    edit. Setup saves the address the API is on, and the API never restarts. Setup ends with the
    first apply, so nothing is pending and HAProxy serves the generated config."""
    finish_setup(server)
    config = docker("exec", server.name, "cat", "/data/config/config.yaml")
    assert f"listen: 0.0.0.0:{PORT}\n" in config

    login = server.call("POST", "/api/v1/auth/login", json={"password": PASSWORD})
    assert login.status_code == 204
    review = server.call("GET", "/api/v1/changes", session_cookie(login)).json()
    assert (review["changes"], review["generated_diff"]) == ([], ""), review
    status = server.call("GET", "/api/v1/status", session_cookie(login)).json()
    assert status["last_apply"]["success"] is True, status
    # The placeholder page answers 200. The generated config moves plain HTTP to HTTPS.
    assert httpx2.get(f"http://{server.ip}/").status_code == 301
    change = {"current_password": PASSWORD, "new_password": NEW_PASSWORD}
    changed = server.call("PUT", "/api/v1/auth/password", session_cookie(login), json=change)
    assert changed.status_code == 204
    cookie = session_cookie(changed)
    assert server.call("GET", "/api/v1/auth/me", cookie).status_code == 200

    settings = server.call("GET", "/api/v1/settings", cookie).json()
    assert settings["network"]["haproxy_lan_ip"] == server.ip
    settings["dns_server"]["forward_unknown_to"] = ["192.168.1.1"]
    assert server.call("PUT", "/api/v1/settings", cookie, json=settings).status_code == 200
    shown = server.call("GET", "/api/v1/settings", cookie).json()
    assert shown["dns_server"]["forward_unknown_to"] == ["192.168.1.1"]
    live = docker("exec", server.name, "cat", "/data/config/config.yaml")
    assert "192.168.1.1" not in live

    logs = server.logs()
    assert logs.count("dashboard listening on") == 1
    assert PASSWORD not in logs
    assert NEW_PASSWORD not in logs
    check_stored_logs(server, cookie, logs)


def check_stored_logs(server: Server, cookie: str, logs: str) -> None:
    """The stored api log copies the container log, without the setup token and the health
    checks, and the stored access log holds the request to HAProxy."""
    stored = server.call("GET", "/api/v1/logs/files/api", cookie)
    assert stored.status_code == 200
    assert "dashboard listening on" in stored.text
    token = re.search(r"enter this setup token:\s+(\S+)", logs)
    assert token is not None
    assert "enter this setup token:" in stored.text
    assert token.group(1) not in stored.text
    assert '"GET /api/v1/healthz HTTP/1.1" 200' in logs
    assert "/api/v1/healthz" not in stored.text
    access = server.call("GET", "/api/v1/logs/files/access", cookie).text
    assert '"GET / HTTP/1.1"' in access


def test_setup_asks_for_the_ports_another_proxy_holds(test_name_prefix: str) -> None:
    """With another program on ports 80 and 443, as when migrating, the setup status asks for
    both, setup takes spare ones, and the first apply succeeds on them."""
    placeholder = "HOMEPROXY_PLACEHOLDER_HTTP_PORT=8081"
    with start_server(f"{test_name_prefix}-ports", placeholder) as server:
        hold = (
            "import socket, time; "
            "held = [socket.create_server(('0.0.0.0', port)) for port in (80, 443)]; "
            "time.sleep(3600)"
        )
        docker("exec", "-d", "-u", "0", server.name, "python3", "-c", hold)
        needs = ["network.http_port", "network.https_port"]
        deadline = time.monotonic() + TIMEOUT
        while (status := server.call("GET", "/api/v1/setup/status").json())["needs"] != needs:
            if time.monotonic() > deadline:
                pytest.fail(f"the setup status never asked for the ports: {status}")
            time.sleep(0.5)
        token = re.search(r"enter this setup token:\s+(\S+)", server.logs())
        assert token is not None
        ports = {"http_port": 8081, "https_port": 8444}
        setup = {"token": token.group(1), "password": PASSWORD, "network": ports}
        response = server.call("POST", "/api/v1/setup", json=setup, timeout=TIMEOUT)
        assert response.status_code == 204, response.text

        login = server.call("POST", "/api/v1/auth/login", json={"password": PASSWORD})
        status = server.call("GET", "/api/v1/status", session_cookie(login)).json()
        assert status["last_apply"]["success"] is True, status
        assert httpx2.get(f"http://{server.ip}:8081/").status_code == 301
