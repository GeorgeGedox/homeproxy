"""A backup downloaded with the backup token restores onto a fresh container.

Two containers of the built image, each its own, in the dev stack's self-signed mode. The source
gets set up, a domain with its certificate, a route and a backup token. A script with only the
token then downloads the archive. The fresh container restores it, restarts its dashboard and
serves the source's routes, certificate, password and dashboard certificate.
"""

import re
from collections.abc import Iterator
from typing import Any

import httpx2
import pytest

from tests.conftest import wait_until
from tests.integration.api.test_api_apply_live import DOMAIN, NOT_YET
from tests.integration.haproxy_live import docker
from tests.integration.image_server import (
    PASSWORD,
    TIMEOUT,
    Server,
    check_detected_values,
    finish_setup,
    session_cookie,
    start_server,
)

SETTINGS = ("HOMEPROXY_DEV_SELF_SIGNED=1", "CLOUDFLARE_BASE_URL=http://127.0.0.1:9")
ROUTE = {
    "version": 1,
    "name": "app",
    "type": "http",
    "hosts": ["app.example.com"],
    "exposure": "lan",
    "upstreams": [{"address": "127.0.0.1:9"}],
}


@pytest.fixture(scope="module")
def source(test_name_prefix: str) -> Iterator[tuple[Server, str]]:
    """A set-up container with a domain, its certificate and a route, and a session cookie."""
    with start_server(f"{test_name_prefix}-backup-source", *SETTINGS) as server:
        finish_setup(server)
        login = server.call("POST", "/api/v1/auth/login", json={"password": PASSWORD})
        assert login.status_code == 204
        yield server, session_cookie(login)


def call(server: Server, cookie: str, method: str, path: str, **kwargs: Any) -> Any:
    """One API call that must succeed. An apply can pass httpx's 5 s default under load."""
    response = server.call(method, f"/api/v1{path}", cookie, timeout=TIMEOUT, **kwargs)
    assert response.status_code < 300, response.text
    return response.json() if response.content else None


def certificate_names(server: Server, cookie: str) -> list[str]:
    """The names of the certificates the dashboard lists."""
    return [c["name"] for c in call(server, cookie, "GET", "/certificates")]


def dashboard_certificate(server: Server) -> str:
    """The certificate part of the dashboard's TLS file, which holds the key as well."""
    return docker(
        "exec", server.name, "sed", "-n", "/BEGIN CERTIFICATE/,$p", "/data/tls/dashboard.pem"
    )


def test_a_backup_made_with_the_token_restores_on_a_fresh_container(
    source: tuple[Server, str], test_name_prefix: str
) -> None:
    server, cookie = source
    call(server, cookie, "POST", "/domains", json=DOMAIN)
    call(server, cookie, "POST", "/routes", json=ROUTE)
    assert call(server, cookie, "POST", "/apply")["success"] is True
    wait_until(
        lambda: certificate_names(server, cookie) == ["example.com"],
        "the certificate",
        timeout=TIMEOUT,
        interval=0.5,
        not_yet=NOT_YET,
    )
    token = call(server, cookie, "POST", "/backup/token")["token"]

    # The script: only the token, no session, no Origin header.
    script = {"Authorization": f"Bearer {token}"}
    download = httpx2.get(
        f"{server.url}/api/v1/backup", headers=script, verify=server.verify, timeout=TIMEOUT
    )
    assert download.status_code == 200
    assert download.headers["content-type"] == "application/gzip"
    assert "set-cookie" not in download.headers
    other = httpx2.get(f"{server.url}/api/v1/settings", headers=script, verify=server.verify)
    assert other.status_code == 401
    assert token not in server.logs()
    stored_log = server.call("GET", "/api/v1/logs/files/api", cookie).text
    assert token not in stored_log
    assert "backup downloaded from" in stored_log

    with start_server(f"{test_name_prefix}-backup-fresh", *SETTINGS) as fresh:
        setup_token = re.search(r"enter this setup token:\s+(\S+)", fresh.logs())
        assert setup_token is not None
        check_detected_values(fresh)
        restored = httpx2.post(
            f"{fresh.url}/api/v1/setup/restore",
            content=download.content,
            headers={"Origin": fresh.url, "X-Setup-Token": setup_token.group(1)},
            verify=fresh.verify,
            timeout=TIMEOUT,
        )
        assert restored.status_code == 204, restored.text
        # The dashboard restarts to load the restored certificate, which is the source's.
        fresh.verify = True
        fresh.wait_for_start(2)
        assert dashboard_certificate(fresh) == dashboard_certificate(server)

        login = fresh.call("POST", "/api/v1/auth/login", json={"password": PASSWORD})
        assert login.status_code == 204
        restored_cookie = session_cookie(login)
        routes = call(fresh, restored_cookie, "GET", "/routes")
        assert [route["name"] for route in routes] == ["app"]
        assert certificate_names(fresh, restored_cookie) == ["example.com"]
        assert call(fresh, restored_cookie, "GET", "/backup/token")["set"] is True
        again = httpx2.get(
            f"{fresh.url}/api/v1/backup", headers=script, verify=fresh.verify, timeout=TIMEOUT
        )
        assert again.status_code == 200


def test_a_restore_on_a_set_up_container_signs_out_and_takes_the_backups_password(
    source: tuple[Server, str], test_name_prefix: str
) -> None:
    server, cookie = source
    archive = server.call("GET", "/api/v1/backup", cookie, timeout=TIMEOUT)
    assert archive.status_code == 200

    new_password = "a password the backup does not have"  # noqa: S105
    with start_server(f"{test_name_prefix}-backup-target", *SETTINGS) as target:
        finish_setup(target)
        login = target.call("POST", "/api/v1/auth/login", json={"password": PASSWORD})
        old_cookie = session_cookie(login)
        change = {"current_password": PASSWORD, "new_password": new_password}
        call(target, old_cookie, "PUT", "/auth/password", json=change)
        own = target.call("POST", "/api/v1/auth/login", json={"password": new_password})
        old_cookie = session_cookie(own)

        restored = target.call(
            "POST",
            "/api/v1/backup/restore",
            old_cookie,
            content=archive.content,
            timeout=TIMEOUT,
        )
        assert restored.status_code == 204, restored.text
        # The dashboard restarts to load the restored certificate, which is the source's.
        target.verify = True
        target.wait_for_start(2)

        assert target.call("GET", "/api/v1/settings", old_cookie).status_code == 401
        wrong = target.call("POST", "/api/v1/auth/login", json={"password": new_password})
        assert wrong.status_code == 401
        right = target.call("POST", "/api/v1/auth/login", json={"password": PASSWORD})
        assert right.status_code == 204
