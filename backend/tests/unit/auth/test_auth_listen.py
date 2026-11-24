"""The confirm-or-revert window of a new `dashboard.listen`, with a short window on a real server.

The built image keeps the parts that need it: a confirm saves, and a restart in the window keeps
the old address (`tests/integration/auth/test_dashboard_listen_live.py`).
"""

import http.client
import json
import socket
import ssl
import time
from pathlib import Path
from typing import Any

import pytest

from homeproxy.auth.sessions import COOKIE_NAME
from homeproxy.store.files import Store
from tests.conftest import free_port
from tests.unit.auth.dashboard_server import PASSWORD, request, running, write_files

WINDOW = 3.0
OLD_LISTEN = "127.0.0.1:8443"


def dashboard_settings(port: int, context: ssl.SSLContext, session: str) -> dict[str, Any]:
    """GET the dashboard settings with a session."""
    connection = http.client.HTTPSConnection("127.0.0.1", port, context=context, timeout=10)
    connection.request("GET", "/api/v1/settings/dashboard", headers={"Cookie": session})
    body: dict[str, Any] = json.loads(connection.getresponse().read())
    connection.close()
    return body


def listening(port: int) -> bool:
    """Whether anything accepts a connection on `port`."""
    try:
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
    except OSError:
        return False
    return True


def test_a_new_address_without_a_session_closes_after_the_window(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Requests without a valid session reach the new address but do not confirm it, nor does a
    session on the old address. At the deadline the new address closes and nothing is saved."""
    write_files(tmp_path, listen=OLD_LISTEN)
    with running(tmp_path, confirm_seconds=WINDOW) as (port, context):
        login = request(port, context, "POST", "/api/v1/auth/login", {"password": PASSWORD})
        session = (login.getheader("set-cookie") or "").split(";", 1)[0]
        new_port = free_port()
        new_listen = f"127.0.0.1:{new_port}"
        body = {"listen": new_listen, "session_ttl": "12h"}
        put = request(port, context, "PUT", "/api/v1/settings/dashboard", body, {"Cookie": session})
        assert put.status == 200

        for headers in [None, {"Cookie": f"{COOKIE_NAME}=forged"}]:
            me = request(new_port, context, "GET", "/api/v1/auth/me", headers=headers)
            assert me.status == 401
        during = dashboard_settings(port, context, session)
        assert (during["listen"], during["pending_listen"]["listen"]) == (OLD_LISTEN, new_listen)

        deadline = time.monotonic() + WINDOW + 10
        while listening(new_port):
            assert time.monotonic() < deadline, "the new address did not close"
            time.sleep(0.1)
        after = dashboard_settings(port, context, session)
        assert (after["listen"], after["pending_listen"]) == (OLD_LISTEN, None)
    assert Store(tmp_path).load_config().dashboard.listen == OLD_LISTEN
    assert "not confirmed in time" in caplog.text


def test_a_staged_route_does_not_stop_a_confirm(tmp_path: Path) -> None:
    """A confirm writes `config.yaml` at once, so only a staged `config.yaml` stops it. A route
    staged in the window does not."""
    write_files(tmp_path, listen=OLD_LISTEN)
    with running(tmp_path, confirm_seconds=30) as (port, context):
        login = request(port, context, "POST", "/api/v1/auth/login", {"password": PASSWORD})
        session = {"Cookie": (login.getheader("set-cookie") or "").split(";", 1)[0]}
        new_port = free_port()
        new_listen = f"127.0.0.1:{new_port}"
        body = {"listen": new_listen, "session_ttl": "12h"}
        assert (
            request(port, context, "PUT", "/api/v1/settings/dashboard", body, session).status == 200
        )
        route = {
            "version": 1,
            "name": "staged",
            "type": "http",
            "hosts": ["staged.example.com"],
            "exposure": "lan",
            "upstreams": [{"address": "192.168.1.20:8080"}],
        }
        assert request(port, context, "POST", "/api/v1/routes", route, session).status == 201

        there = request(new_port, context, "POST", "/api/v1/auth/login", {"password": PASSWORD})
        new_session = {"Cookie": (there.getheader("set-cookie") or "").split(";", 1)[0]}
        assert (
            request(new_port, context, "GET", "/api/v1/auth/me", headers=new_session).status == 200
        )
    assert Store(tmp_path).load_config().dashboard.listen == new_listen
