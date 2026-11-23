"""Tests of the dashboard server: listen address, real HTTPS, and the source filter.

The servers here are real uvicorn servers on 127.0.0.1 with a port the kernel picks.
"""

import http.client
import socket
import ssl
from pathlib import Path

import pytest

from homeproxy.auth import server as server_module
from homeproxy.auth.runtime import AuthRuntime
from homeproxy.auth.server import (
    BOOTSTRAP_ENV,
    ServerConfigError,
    bootstrap_listen,
    default_route_address,
    plan_listen,
)
from homeproxy.auth.sessions import COOKIE_NAME
from homeproxy.auth.sources import lan_policy, private_policy
from homeproxy.store.files import Store
from tests.conftest import free_port
from tests.unit.auth.dashboard_server import PASSWORD, request, running, write_files

# Listen address -------------------------------------------------------------------------------


def test_setup_mode_uses_the_bootstrap_variable(tmp_path: Path) -> None:
    """Without config.yaml, HOMEPROXY_BOOTSTRAP_LISTEN wins and only private ranges connect."""
    plan = plan_listen(Store(tmp_path), {BOOTSTRAP_ENV: "192.168.1.10:9443"})
    assert (plan.host, plan.port, plan.setup_mode) == ("192.168.1.10", 9443, True)
    assert AuthRuntime(Store(tmp_path)).source_policy() == private_policy()


def test_setup_mode_defaults_to_the_default_route_address(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the variable, the dashboard listens on port 8443 of the default route's address."""
    monkeypatch.setattr(server_module, "default_route_address", lambda: "10.20.30.40")
    plan = plan_listen(Store(tmp_path), {})
    assert (plan.host, plan.port) == ("10.20.30.40", 8443)


def test_no_default_route_falls_back_to_every_address(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no default route, setup listens everywhere, still private ranges only."""
    monkeypatch.setattr(server_module, "default_route_address", lambda: None)
    assert bootstrap_listen({}) == ("0.0.0.0", 8443)  # noqa: S104


def test_default_route_address_is_a_local_address() -> None:
    """The probe returns an address of this host, or None without a route. It sends nothing."""
    address = default_route_address()
    if address is None:
        pytest.skip("this host has no default route")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind((address, 0))


@pytest.mark.parametrize(
    "value",
    ["8443", "0.0.0.0", "example.com:8443", "1.2.3.4:0", "1.2.3.4:70000", "::1:8443"],  # noqa: S104
)
def test_bad_bootstrap_variable_stops_the_start(value: str) -> None:
    """A wrong variable fails with a clear message instead of listening somewhere unexpected."""
    with pytest.raises(ServerConfigError, match=BOOTSTRAP_ENV):
        bootstrap_listen({BOOTSTRAP_ENV: value})


def test_after_setup_the_dashboard_listen_address_and_lan_ranges_apply(tmp_path: Path) -> None:
    """With config.yaml, dashboard.listen and lan_cidrs decide, and the variable is ignored."""
    port = free_port()
    write_files(tmp_path, lan="192.168.1.0/24", listen=f"127.0.0.1:{port}")
    plan = plan_listen(Store(tmp_path), {BOOTSTRAP_ENV: "0.0.0.0:9000"})
    assert (plan.host, plan.port, plan.setup_mode) == ("127.0.0.1", port, False)
    assert AuthRuntime(Store(tmp_path)).source_policy() == lan_policy(["192.168.1.0/24"])


def test_unbindable_dashboard_listen_falls_back_to_the_setup_address(tmp_path: Path) -> None:
    """When the host lacks the saved address, as after a change of its LAN IP, the dashboard uses
    the setup address instead of failing to start."""
    port = free_port()
    write_files(tmp_path, listen="192.0.2.1:8443")
    plan = plan_listen(Store(tmp_path), {BOOTSTRAP_ENV: f"127.0.0.1:{port}"})
    assert (plan.host, plan.port, plan.setup_mode, plan.reason) == (
        "127.0.0.1",
        port,
        False,
        "fallback",
    )


def test_a_stale_bootstrap_variable_falls_back_to_every_address(tmp_path: Path) -> None:
    """When `HOMEPROXY_BOOTSTRAP_LISTEN` names the old LAN IP too, the fallback listens on every
    address on its port, still private ranges only."""
    write_files(tmp_path, listen="192.0.2.1:8443")
    plan = plan_listen(Store(tmp_path), {BOOTSTRAP_ENV: "192.0.2.1:9443"})
    assert (plan.host, plan.port, plan.reason) == ("0.0.0.0", 9443, "fallback")  # noqa: S104


def test_broken_dashboard_settings_fall_back_to_recovery(tmp_path: Path) -> None:
    """A config.yaml with unusable dashboard settings keeps the dashboard reachable from private
    ranges on the bootstrap address, without opening setup."""
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.yaml").write_text("version: 1\ndashboard: {}\n")
    plan = plan_listen(Store(tmp_path), {BOOTSTRAP_ENV: "127.0.0.1:9443"})
    assert (plan.host, plan.port, plan.setup_mode, plan.reason) == (
        "127.0.0.1",
        9443,
        False,
        "recovery",
    )
    assert AuthRuntime(Store(tmp_path)).source_policy() == private_policy()


# Real server ----------------------------------------------------------------------------------


def test_login_works_over_https_and_x_forwarded_for_cannot_dodge_the_throttle(
    tmp_path: Path,
) -> None:
    """Over a real TLS connection with a verified certificate, login works. uvicorn trusts
    X-Forwarded-For from 127.0.0.1 unless told not to, so only a real server shows that a client
    cannot pose as another one to dodge the throttle."""
    write_files(tmp_path)
    with running(tmp_path) as (port, context):
        assert request(port, context, "GET", "/api/v1/healthz").status == 200
        ok = request(port, context, "POST", "/api/v1/auth/login", {"password": PASSWORD})
        assert ok.status == 204
        cookie = ok.getheader("set-cookie") or ""
        assert cookie.startswith(COOKIE_NAME)
        session = cookie.split(";", 1)[0]
        me = request(port, context, "GET", "/api/v1/auth/me", headers={"Cookie": session})
        assert me.status == 200
        for number in range(5):
            wrong = request(
                port,
                context,
                "POST",
                "/api/v1/auth/login",
                {"password": "wrong password"},
                headers={"X-Forwarded-For": f"192.168.7.{number}"},
            )
            assert wrong.status == 401
        sixth = request(port, context, "POST", "/api/v1/auth/login", {"password": PASSWORD})
        assert sixth.status == 429


def test_responses_forbid_framing(tmp_path: Path) -> None:
    """Every response tells the browser not to frame the dashboard."""
    write_files(tmp_path)
    with running(tmp_path) as (port, context):
        for path in ("/", "/api/v1/auth/me"):
            response = request(port, context, "GET", path)
            assert response.getheader("x-frame-options") == "DENY"
            assert response.getheader("content-security-policy") == "frame-ancestors 'none'"


def _no_http_response(port: int) -> str:
    """Connect over TLS, send a request, and return how the connection ended."""
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    connection = http.client.HTTPSConnection("127.0.0.1", port, context=context, timeout=10)
    try:
        connection.request("GET", "/api/v1/healthz")
        response = connection.getresponse()
    except (ConnectionError, ssl.SSLError, http.client.RemoteDisconnected, OSError) as error:
        return type(error).__name__
    finally:
        connection.close()
    return f"HTTP {response.status}"


def test_source_outside_the_policy_gets_no_http_response(tmp_path: Path) -> None:
    """A client outside lan_cidrs never sees an HTTP status line. The ranges are read again on
    each connection, so adding or removing one needs no restart."""
    write_files(tmp_path, lan="192.168.1.0/24")
    with running(tmp_path) as (port, _):
        outcome = _no_http_response(port)
        assert not outcome.startswith("HTTP"), outcome
        write_files(tmp_path, lan="127.0.0.0/8")
        assert _no_http_response(port) == "HTTP 200"
        write_files(tmp_path, lan="192.168.1.0/24")
        outcome = _no_http_response(port)
        assert not outcome.startswith("HTTP"), outcome


def test_recovery_ends_once_the_file_is_fixed(tmp_path: Path) -> None:
    """With unusable dashboard settings private ranges may connect; once the file is fixed only
    lan_cidrs may, without a restart."""
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.yaml").write_text("version: 1\ndashboard: {}\n")
    with running(tmp_path) as (port, _):
        assert _no_http_response(port) == "HTTP 200"
        write_files(tmp_path, lan="192.168.1.0/24")
        outcome = _no_http_response(port)
        assert not outcome.startswith("HTTP"), outcome


def test_plain_http_gets_no_http_response(tmp_path: Path) -> None:
    """The dashboard speaks only HTTPS."""
    write_files(tmp_path)
    with running(tmp_path) as (port, _):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        with pytest.raises((ConnectionError, http.client.HTTPException, OSError)):
            connection.request("GET", "/api/v1/healthz")
            connection.getresponse()
        connection.close()
