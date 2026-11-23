"""A new `dashboard.listen` in the built image: saved only once an admin's session reaches it.

The container listens on 8443, published on a random loopback port. The new address, 9443, is not
published, so it is reached from inside the container on the container's own address, which is in
`network.lan_cidrs`. No host port is bound.
"""

import json
from typing import Any

from homeproxy.store.tree import parse_tree
from tests.conftest import wait_until
from tests.integration.dashboard_client import CLIENT_SCRIPT, PYTHON
from tests.integration.haproxy_live import docker
from tests.integration.image_server import (
    PASSWORD,
    Server,
    finish_setup,
    session_cookie,
    start_server,
)

OLD_PORT = 8443
NEW_PORT = 9443
OLD_LISTEN = f"0.0.0.0:{OLD_PORT}"
NEW_LISTEN = f"0.0.0.0:{NEW_PORT}"
MISSING = "10.255.255.1:8443"
"""An address the container does not have, as after a change of the host's LAN IP."""
CONFIG = "/data/config/config.yaml"
WAIT = 30.0
"""A restart of the API, or the close of the old address, on a slow machine."""


def inside(server: Server, port: int, steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Send requests to `port` from inside the container, as a LAN client would."""
    args = [server.ip, str(port), json.dumps(steps)]
    out = docker("exec", server.name, PYTHON, "-c", CLIENT_SCRIPT, *args)
    results: list[dict[str, Any]] = json.loads(out)
    return results


def answers(server: Server, port: int) -> bool:
    """Whether `port` gives any HTTP response."""
    return "status" in inside(server, port, [{"method": "GET", "path": "/api/v1/healthz"}])[0]


def saved_listen(server: Server) -> str:
    """`dashboard.listen` in the container's `config.yaml`."""
    config = docker("exec", server.name, "cat", "/data/config/config.yaml")
    listen: str = parse_tree(config.encode(), "config.yaml")["dashboard"]["listen"]
    return listen


def propose(server: Server, cookie: str) -> None:
    """PUT the new address and check that it answers beside the saved one."""
    body = {"listen": NEW_LISTEN, "session_ttl": "12h"}
    response = server.call("PUT", "/api/v1/settings/dashboard", cookie, json=body)
    assert response.status_code == 200, response.text
    answer = response.json()
    assert answer["listen"] == OLD_LISTEN
    assert answer["pending_listen"]["url"] == f"https://127.0.0.1:{NEW_PORT}"
    assert answers(server, NEW_PORT)
    assert saved_listen(server) == OLD_LISTEN


def restart_api(server: Server) -> int:
    """Restart the API service and return the start count to wait for."""
    count = server.logs().count("dashboard listening on") + 1
    docker("exec", server.name, "/command/s6-svc", "-r", "/run/service/api")
    return count


def test_a_new_listen_address_is_saved_only_once_a_session_reaches_it(
    test_name_prefix: str,
) -> None:
    """1. A restart in the window keeps the old address. 2. The old cookie is not sent to a new
    host, so the admin logs in there; the next call saves the address, the old address stops
    answering, and the next start listens only on the new one.

    Requests without a valid session, and the close at the deadline, are proved with a short
    window in `tests/unit/auth/test_auth_listen.py`."""
    with start_server(f"{test_name_prefix}-listen") as server:
        finish_setup(server)
        login = server.call("POST", "/api/v1/auth/login", json={"password": PASSWORD})
        cookie = session_cookie(login)

        # 1. Restart in the window.
        propose(server, cookie)
        server.wait_for_start(restart_api(server))
        assert not answers(server, NEW_PORT)
        assert saved_listen(server) == OLD_LISTEN

        # 2. Login on the new address, then any call with that session.
        propose(server, cookie)
        origin = {"Origin": f"https://{server.ip}:{NEW_PORT}"}
        login_step = {"method": "POST", "path": "/api/v1/auth/login", "headers": origin}
        first = inside(server, NEW_PORT, [{**login_step, "body": {"password": PASSWORD}}])[0]
        assert first["status"] == 204
        set_cookie = next(value for key, value in first["headers"] if key == "set-cookie")
        assert saved_listen(server) == OLD_LISTEN
        new_session = {"Cookie": set_cookie.split(";", 1)[0]}
        me = {"method": "GET", "path": "/api/v1/auth/me", "headers": new_session}
        assert inside(server, NEW_PORT, [me])[0]["status"] == 200
        assert saved_listen(server) == NEW_LISTEN
        settings = {"method": "GET", "path": "/api/v1/settings/dashboard", "headers": new_session}
        shown = json.loads(inside(server, NEW_PORT, [settings])[0]["body"])
        assert (shown["listen"], shown["pending_listen"]) == (NEW_LISTEN, None)
        # The old address closes a few seconds after the confirmation, without a restart.
        wait_until(
            lambda: not answers(server, OLD_PORT),
            "the old address to close",
            timeout=WAIT,
            interval=1.0,
        )
        assert answers(server, NEW_PORT)

        count = restart_api(server)
        wait_until(
            lambda: (
                server.logs().count("dashboard listening on") >= count and answers(server, NEW_PORT)
            ),
            "the start on the new address",
            timeout=WAIT,
            interval=1.0,
        )
        assert f"dashboard listening on {NEW_LISTEN} (dashboard.listen)" in server.logs()
        assert not answers(server, OLD_PORT)
        assert PASSWORD not in server.logs()


def test_an_address_the_host_lacks_falls_back_to_the_setup_address(test_name_prefix: str) -> None:
    """After a change of the host's LAN IP, `dashboard.listen` and `network.lan_cidrs` are stale.
    The dashboard answers on the setup address to private sources, and logins work. Saving the
    address the request arrived on needs no confirmation, and the next start listens there."""
    with start_server(f"{test_name_prefix}-fallback") as server:
        finish_setup(server)
        lan = parse_tree(docker("exec", server.name, "cat", CONFIG).encode(), "config.yaml")
        lan_cidr = lan["network"]["lan_cidrs"][0]
        edits = [f"s/{OLD_LISTEN}/{MISSING}/", f"s|{lan_cidr}|192.168.99.0/24|"]
        sed(server, edits)
        assert saved_listen(server) == MISSING

        server.wait_for_start(restart_api(server))

        login = server.call("POST", "/api/v1/auth/login", json={"password": PASSWORD})
        assert login.status_code == 204
        assert "(fallback), accepting private ranges" in server.logs()

        here = f"{server.ip}:{OLD_PORT}"
        body = {"listen": here, "session_ttl": "12h"}
        put = server.call("PUT", "/api/v1/settings/dashboard", session_cookie(login), json=body)
        assert put.status_code == 200, put.text
        assert (put.json()["listen"], put.json()["pending_listen"]) == (here, None)
        assert saved_listen(server) == here

        sed(server, [f"s|192.168.99.0/24|{lan_cidr}|"])
        server.wait_for_start(restart_api(server))
        assert server.logs().count("(fallback)") == 1
        assert f"dashboard listening on {here} (dashboard.listen)" in server.logs()


def sed(server: Server, edits: list[str]) -> None:
    """Edit `config.yaml` in place, as the admin would by hand."""
    script = [arg for edit in edits for arg in ("-e", edit)]
    docker("exec", "-u", "homeproxy", server.name, "sed", "-i", *script, CONFIG)
