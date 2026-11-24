"""`POST /setup` and the setup status fill in the network values HomeProxy detects."""

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from ruamel.yaml import YAML

from homeproxy.auth import setup_detection
from homeproxy.auth.runtime import ensure_session_key, get_runtime
from homeproxy.auth.server import BOOTSTRAP_ENV
from homeproxy.store import Store
from tests.integration.api.conftest import PASSWORD, make_client

LAN_IP = "192.168.1.10"
# /proc/net/route on a host whose default route leaves eth0 on 192.168.1.0/24.
ROUTES = (
    "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
    "eth0\t00000000\t0101A8C0\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"
    "eth0\t0001A8C0\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0\n"
)


def detect(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, routes: str = ROUTES) -> None:
    """Make detection see `LAN_IP` as the default route's address, with `routes` as the table,
    and the setup-mode server listen on `LAN_IP:8443`."""
    table = tmp_path / "route"
    table.write_text(routes)
    monkeypatch.setattr(setup_detection, "ROUTE_TABLE", table)
    monkeypatch.setattr(setup_detection, "default_route_address", lambda: LAN_IP)
    monkeypatch.setenv(BOOTSTRAP_ENV, f"{LAN_IP}:8443")


def setup_mode(root: Path, client: str) -> tuple[TestClient, str]:
    """The real app in setup mode on `root`, reached from `client`, and the setup token."""
    test = make_client(root, client)
    assert isinstance(test.app, FastAPI)
    token = get_runtime(test.app).setup_token
    assert token is not None
    return test, token.value


def written(root: Path) -> dict[str, object]:
    """The `config.yaml` setup wrote, without the password hash."""
    config: dict[str, dict[str, object]] = YAML(typ="safe").load(root / "config" / "config.yaml")
    config["dashboard"].pop("password_hash")
    return dict(config)


@pytest.mark.parametrize(
    ("client", "lan_cidrs"),
    [
        pytest.param("192.168.1.50", ["192.168.1.0/24"], id="client-on-the-lan"),
        pytest.param("10.20.0.5", ["192.168.1.0/24", "10.20.0.5"], id="client-on-another-vlan"),
    ],
)
def test_setup_with_only_token_and_password_writes_the_detected_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, client: str, lan_cidrs: list[str]
) -> None:
    """The status reports the detected values, setup writes them, and a client outside the LAN
    range is added to it, so it can reach the dashboard afterwards."""
    detect(monkeypatch, tmp_path)
    test, token = setup_mode(tmp_path / "data", client)
    detected = {
        "network": {"lan_cidrs": ["192.168.1.0/24"], "haproxy_lan_ip": LAN_IP},
        "dns_server": {"listen": f"{LAN_IP}:5335"},
    }
    status = test.get("/api/v1/setup/status").json()
    assert status == {"setup_required": True, "detected": detected, "needs": []}

    response = test.post("/api/v1/setup", json={"token": token, "password": PASSWORD})
    assert response.status_code == 204
    assert written(tmp_path / "data") == {
        "version": 1,
        "dashboard": {"listen": f"{LAN_IP}:8443"},
        "network": {"lan_cidrs": lan_cidrs, "haproxy_lan_ip": LAN_IP},
        "dns_server": {"listen": f"{LAN_IP}:5335", "forward_unknown_to": ["1.1.1.1"]},
    }
    assert test.get("/api/v1/setup/status").json() == {"setup_required": False}


def test_a_failed_first_apply_does_not_fail_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Setup runs the first apply. With no HAProxy here it fails, setup still succeeds, and the
    failure shows as the last apply."""
    detect(monkeypatch, tmp_path)
    root = tmp_path / "data"
    # What the image has before setup: the placeholder release and the session key.
    (root / "releases" / "placeholder").mkdir(parents=True)
    (root / "generated").symlink_to("releases/placeholder")
    ensure_session_key(Store(root))
    test, token = setup_mode(root, "192.168.1.50")
    response = test.post("/api/v1/setup", json={"token": token, "password": PASSWORD})
    assert response.status_code == 204
    assert test.post("/api/v1/auth/login", json={"password": PASSWORD}).status_code == 204
    last_apply = test.get("/api/v1/status").json()["last_apply"]
    assert last_apply["success"] is False, last_apply


def test_setup_queues_the_first_geoip_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    detect(monkeypatch, tmp_path)
    root = tmp_path / "data"
    (root / "releases" / "placeholder").mkdir(parents=True)
    (root / "generated").symlink_to("releases/placeholder")
    ensure_session_key(Store(root))
    test, token = setup_mode(root, "192.168.1.50")
    response = test.post("/api/v1/setup", json={"token": token, "password": PASSWORD})
    assert response.status_code == 204
    assert test.post("/api/v1/auth/login", json={"password": PASSWORD}).status_code == 204

    jobs = test.get("/api/v1/jobs").json()
    assert [(job["kind"], job["state"]) for job in jobs] == [("geoip.refresh", "queued")]


def test_a_value_that_cannot_be_detected_must_be_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a connected route the LAN range is not detected: the status lists it, setup
    without it is refused naming it, and the same token then works with it. A sent value wins
    over a detected one. The request cannot choose the dashboard address."""
    detect(monkeypatch, tmp_path, routes="")
    test, token = setup_mode(tmp_path / "data", "192.168.1.50")
    status = test.get("/api/v1/setup/status").json()
    assert "lan_cidrs" not in status["detected"]["network"]
    assert status["needs"] == ["network.lan_cidrs"]

    refused = test.post("/api/v1/setup", json={"token": token, "password": PASSWORD})
    assert refused.status_code == 422
    assert refused.json()["error"] == {
        "code": "validation_failed",
        "message": "network.lan_cidrs: HomeProxy cannot detect this value. Send it.",
        "field": "network.lan_cidrs",
    }
    assert not (tmp_path / "data" / "config" / "config.yaml").exists()

    sent = {"network": {"lan_cidrs": ["192.168.0.0/16"]}, "dns_server": {"forward_unknown_to": []}}
    body = {"token": token, "password": PASSWORD, **sent}
    moved = test.post("/api/v1/setup", json={**body, "dashboard_listen": f"{LAN_IP}:9443"})
    assert moved.status_code == 422
    assert test.post("/api/v1/setup", json=body).status_code == 204
    assert written(tmp_path / "data") == {
        "version": 1,
        "dashboard": {"listen": f"{LAN_IP}:8443"},
        "network": {"lan_cidrs": ["192.168.0.0/16"], "haproxy_lan_ip": LAN_IP},
        "dns_server": {"listen": f"{LAN_IP}:5335", "forward_unknown_to": []},
    }


def test_a_default_port_another_program_holds_must_be_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With another program on 443, the status asks for the HTTPS port, setup without it is
    refused naming it, and setup with a free one writes it."""
    detect(monkeypatch, tmp_path)
    monkeypatch.setattr(setup_detection, "port_in_use", lambda port, address: port == 443)
    test, token = setup_mode(tmp_path / "data", "192.168.1.50")
    assert test.get("/api/v1/setup/status").json()["needs"] == ["network.https_port"]

    refused = test.post("/api/v1/setup", json={"token": token, "password": PASSWORD})
    assert refused.status_code == 422
    assert refused.json()["error"]["field"] == "network.https_port"
    assert not (tmp_path / "data" / "config" / "config.yaml").exists()

    body = {"token": token, "password": PASSWORD, "network": {"https_port": 8444}}
    assert test.post("/api/v1/setup", json=body).status_code == 204
    assert written(tmp_path / "data")["network"] == {
        "https_port": 8444,
        "lan_cidrs": ["192.168.1.0/24"],
        "haproxy_lan_ip": LAN_IP,
    }
