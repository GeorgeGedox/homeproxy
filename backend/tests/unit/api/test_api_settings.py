"""Settings on the real app: `/settings` is staged, never written live, and `/settings/dashboard`
is written at once."""

import re
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from homeproxy.haproxy.pipeline import ApplyResult, Pipeline
from homeproxy.jobs import JobRunner
from tests.conftest import free_port

DASHBOARD = {"listen": "192.168.1.10:8443", "session_ttl": "12h"}
"""The dashboard settings of `data_dir`."""


def test_a_settings_change_is_staged_with_only_the_changed_keys(
    api: TestClient, data_dir: Path
) -> None:
    """PUT stages `config.yaml` in pending with the new values, keeps comments, adds no defaults,
    and leaves `/data/config` byte-identical. GET then shows the pending value. A key set back to
    its default is removed."""
    live = data_dir / "config" / "config.yaml"
    staged = data_dir / "pending" / "files" / "config.yaml"
    before = live.read_bytes()
    current = api.get("/api/v1/settings").json()
    assert current["network"]["lan_cidrs"] == ["192.168.1.0/24"]
    assert current["geoip"] == {"provider": "dbip-lite", "block_countries": []}
    assert current["certificates"] == {"staging": False}

    unchanged = api.put("/api/v1/settings", json=current)
    assert (unchanged.status_code, unchanged.json()) == (200, {"changed": False, "warnings": []})
    assert not staged.exists()

    current["dns_server"]["forward_unknown_to"] = ["192.168.1.1"]
    current["cloudflare"]["enabled"] = True
    current["certificates"]["staging"] = True
    assert api.put("/api/v1/settings", json=current).json()["changed"] is True

    assert live.read_bytes() == before
    text = staged.read_text()
    assert "# Hand-written comment that every edit keeps." in text
    assert re.search(r"lan_cidrs: \[192\.168\.1\.0/24\] +# home LAN", text)
    assert "192.168.1.1" in text
    assert "certificates:\n  staging: true" in text
    for default in ("http_port", "public_ip", "provider", "crowdsec"):
        assert default not in text
    shown = api.get("/api/v1/settings").json()
    assert shown["dns_server"]["forward_unknown_to"] == ["192.168.1.1"]
    assert shown["cloudflare"] == {"enabled": True, "reconcile_interval": "15m"}
    assert shown["certificates"] == {"staging": True}

    # A value set back to its default, and the defaults inside a changed value, are not written.
    shown["cloudflare"]["enabled"] = False
    shown["notifications"]["targets"] = [{"name": "discord-main", "type": "discord"}]
    assert api.put("/api/v1/settings", json=shown).status_code == 200
    text = staged.read_text()
    assert "cloudflare" not in text
    assert "discord-main" in text
    assert "events" not in text


def test_a_public_lan_range_is_saved_with_a_warning(api: TestClient) -> None:
    current = api.get("/api/v1/settings").json()
    current["network"]["lan_cidrs"].append("0.0.0.0/1")

    response = api.put("/api/v1/settings", json=current)

    warning = "network.lan_cidrs: 0.0.0.0/1 is a public range, so its clients count as LAN"
    assert response.json() == {"changed": True, "warnings": [warning]}
    assert api.get("/api/v1/settings").json()["network"]["lan_cidrs"][-1] == "0.0.0.0/1"


@pytest.mark.parametrize(
    ("change", "field", "message"),
    [
        pytest.param(
            {"network": {"http_port": 5335}},
            "network.http_port",
            "dns_server.listen port is already used by http_port",
            id="haproxy-port-on-dns",
        ),
        pytest.param(
            {"dns_server": {"listen": "192.168.1.10:8443"}},
            "dns_server.listen",
            "dns_server.listen 192.168.1.10:8443 and the dashboard's address 192.168.1.10:8443 "
            "share port 8443; give one of them another port",
            id="dns-on-dashboard",
        ),
        pytest.param(
            {"prometheus": {"enabled": True, "listen": "192.168.1.10:5335"}},
            "prometheus.listen",
            "prometheus.listen 192.168.1.10:5335 and the dns_server's address 192.168.1.10:5335 "
            "share port 5335; give one of them another port",
            id="prometheus-on-dns",
        ),
    ],
)
def test_a_port_clash_is_refused_at_save(
    api: TestClient, change: dict[str, dict[str, object]], field: str, message: str
) -> None:
    """The apply's port rules refuse the save, naming the changed setting. Nothing is staged."""
    current = api.get("/api/v1/settings").json()
    body = current | {name: current[name] | values for name, values in change.items()}

    response = api.put("/api/v1/settings", json=body)

    assert response.status_code == 422
    assert response.json()["error"] == {"code": "port_clash", "message": message, "field": field}
    assert api.get("/api/v1/changes").json()["changes"] == []


def test_a_settings_save_checks_the_block_list_against_the_geoip_data(api: TestClient) -> None:
    """The same check as `PUT /geoip`, so the settings are no way around it."""
    current = api.get("/api/v1/settings").json()
    current["geoip"]["block_countries"] = ["CN"]

    response = api.put("/api/v1/settings", json=current)

    assert response.status_code == 422
    assert response.json()["error"]["field"] == "geoip.block_countries.0"


def test_a_hand_broken_config_is_reported_without_its_content(
    api: TestClient, data_dir: Path
) -> None:
    """A bad value in `config.yaml` gives 422 naming the field, never the value."""
    live = data_dir / "config" / "config.yaml"
    live.write_text(
        live.read_text().replace("haproxy_lan_ip: 192.168.1.10", "haproxy_lan_ip: x$ecret")
    )
    response = api.get("/api/v1/settings")
    assert response.status_code == 422
    assert response.json()["error"]["field"] == "network.haproxy_lan_ip"
    assert "x$ecret" not in response.text


def test_prometheus_and_the_newer_fields_round_trip(api: TestClient) -> None:
    """The Prometheus section, the public IP, the captcha provider, AppSec, the notification
    targets, the heartbeat and the reconcile interval save and read back. The heartbeat turns on
    only with its URL."""
    current = api.get("/api/v1/settings").json()
    assert current["prometheus"] == {"enabled": False, "listen": None}
    assert current["notifications"] == {
        "targets": [],
        "heartbeat": {"enabled": False, "interval": "1m"},
    }
    current["prometheus"] = {"enabled": True, "listen": "192.168.1.10:8405"}
    current["notifications"] = {
        "targets": [{"name": "discord-main", "type": "discord", "events": ["apply_failed"]}],
        "heartbeat": {"enabled": True, "interval": "5m"},
    }
    current["network"]["public_ip"] = "203.0.113.7"
    current["crowdsec"] |= {"captcha_provider": "turnstile", "appsec": True}
    current["cloudflare"]["reconcile_interval"] = "30m"

    refused = api.put("/api/v1/settings", json=current)
    assert refused.status_code == 422
    assert refused.json()["error"]["field"] == "notifications.heartbeat.enabled"
    assert "heartbeat URL" in refused.json()["error"]["message"]
    url = {"heartbeat_url": "https://hc-ping.example.com/b1"}
    assert api.put("/api/v1/notifications/urls", json=url).status_code < 300
    assert api.put("/api/v1/settings", json=current).status_code == 200

    assert api.get("/api/v1/settings").json() == current


def test_a_successful_apply_reschedules_reconcile_at_the_applied_interval(
    api: TestClient, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No HAProxy runs here, so the interval is written as the apply would leave it, and the
    apply only reports success."""
    with (data_dir / "config" / "config.yaml").open("a") as config:
        config.write("cloudflare:\n    reconcile_interval: 30m\n")
    monkeypatch.setattr(Pipeline, "apply", lambda self, user: ApplyResult(True, "done", "none"))
    schedules: list[tuple[str, timedelta]] = []

    def schedule_every(
        self: JobRunner, kind: str, func: Callable[[], object], *, interval: timedelta, **_: object
    ) -> None:
        schedules.append((kind, interval))

    monkeypatch.setattr(JobRunner, "schedule_every", schedule_every)
    assert api.post("/api/v1/apply").json()["success"] is True
    assert schedules == [("cloudflare.reconcile", timedelta(minutes=30))]


def test_the_session_length_is_saved_at_once_and_keeps_the_session(
    api: TestClient, data_dir: Path
) -> None:
    """`session_ttl` is written to the live `config.yaml` with its comments, the current session
    stays valid, and the listen address is untouched."""
    shown = api.get("/api/v1/settings/dashboard").json()
    assert shown == DASHBOARD | {"pending_listen": None}

    saved = api.put("/api/v1/settings/dashboard", json=DASHBOARD | {"session_ttl": "8h"})

    assert saved.status_code == 200
    assert saved.json() == shown | {"session_ttl": "8h"}
    text = (data_dir / "config" / "config.yaml").read_text()
    assert "# Hand-written comment that every edit keeps." in text
    assert "session_ttl: 8h" in text
    assert api.get("/api/v1/auth/me").status_code == 200


def expose_through_a_public_route(data_dir: Path, port: int) -> None:
    """A public `tcp` route whose upstream is the dashboard's next address."""
    route = (
        "version: 1\nname: game\ntype: tcp\nlisten_port: 7000\nexposure: public\n"
        f"upstreams:\n  - address: 127.0.0.1:{port}\n"
    )
    (data_dir / "config" / "routes").mkdir()
    (data_dir / "config" / "routes" / "game.yaml").write_text(route)


def give_haproxy_the_port(data_dir: Path, port: int) -> None:
    """HAProxy's `https_port` is the port, but HAProxy is not running, so the port is free."""
    config = data_dir / "config" / "config.yaml"
    lan_ip = "    haproxy_lan_ip: 192.168.1.10\n"
    config.write_text(config.read_text().replace(lan_ip, f"{lan_ip}    https_port: {port}\n"))


@pytest.mark.parametrize(
    ("hand_edit", "change", "field", "reason"),
    [
        pytest.param(None, {"session_ttl": "4m"}, "session_ttl", "", id="session-too-short"),
        # TEST-NET-3 is never an address of this host, so the bind fails.
        pytest.param(
            None, {"listen": "203.0.113.5:8443"}, "listen", "cannot listen", id="not-on-host"
        ),
        # The saved 192.168.1.10:8443 keeps listening until the move is confirmed.
        pytest.param(
            None,
            {"listen": "0.0.0.0:8443"},
            "listen",
            "Move to another port first, then to 0.0.0.0:8443.",
            id="same-port-as-the-saved-address",
        ),
        pytest.param(
            expose_through_a_public_route,
            {"listen": "127.0.0.1:{port}"},
            "listen",
            "is the dashboard",
            id="exposed-by-a-public-route",
        ),
        pytest.param(
            give_haproxy_the_port,
            {"listen": "127.0.0.1:{port}"},
            "listen",
            "https_port",
            id="haproxy-port-while-haproxy-is-down",
        ),
    ],
)
def test_a_dashboard_change_that_could_lock_the_admin_out_or_expose_it_is_refused(
    api: TestClient,
    data_dir: Path,
    hand_edit: Callable[[Path, int], None] | None,
    change: dict[str, str],
    field: str,
    reason: str,
) -> None:
    """Nothing is saved, and no new address waits for confirmation. The addresses that pass the
    models bind on this host, so only the apply rules can refuse them."""
    port = free_port()
    if hand_edit is not None:
        hand_edit(data_dir, port)
    live = data_dir / "config" / "config.yaml"
    before = live.read_bytes()
    current = api.get("/api/v1/settings/dashboard").json()
    body = DASHBOARD | {key: value.format(port=port) for key, value in change.items()}

    response = api.put("/api/v1/settings/dashboard", json=body)

    assert response.status_code == 422
    assert response.json()["error"]["field"] == field
    assert reason in response.json()["error"]["message"]
    assert live.read_bytes() == before
    assert api.get("/api/v1/settings/dashboard").json() == current


def test_a_dashboard_change_waits_for_pending_edits(api: TestClient) -> None:
    """A staged `config.yaml` holds the old dashboard settings, so an apply would undo a change."""
    settings = api.get("/api/v1/settings").json()
    settings["cloudflare"]["enabled"] = True
    assert api.put("/api/v1/settings", json=settings).status_code == 200

    response = api.put("/api/v1/settings/dashboard", json=DASHBOARD | {"session_ttl": "8h"})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "changes_pending"
    assert api.put("/api/v1/settings/dashboard", json=DASHBOARD).status_code == 200
