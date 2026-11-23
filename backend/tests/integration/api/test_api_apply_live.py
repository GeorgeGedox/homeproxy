"""Domains, routes, apply, certificates, history, status, jobs and audit through the built image.

The container runs in the dev stack's self-signed mode, so adding a domain needs no Cloudflare and
its certificate is made offline. Its Cloudflare address is a closed port, so the reconcile job after
an apply fails. It is its own container, because setup changes its config.
"""

import socket
import ssl
from collections.abc import Iterator
from typing import Any

import pytest
from cryptography import x509

from homeproxy.store.tree import parse_tree
from tests.conftest import wait_until
from tests.integration.haproxy_live import docker
from tests.integration.image_server import (
    PASSWORD,
    TIMEOUT,
    Server,
    finish_setup,
    session_cookie,
    start_server,
)

CLOUDFLARE_DOWN = "example.com: Cloudflare could not be reached, check the internet connection"
TOKEN = "live-test-cloudflare-token"  # noqa: S105 - Cloudflare is a closed port
DOMAIN = {
    "zone": "example.com",
    "dns_provider": "cloudflare",
    "credentials": "cf-main",
    "api_token": TOKEN,
}
ROUTE = {
    "version": 1,
    "name": "app",
    "type": "http",
    "hosts": ["app.example.com"],
    "exposure": "lan",
    "upstreams": [{"address": "127.0.0.1:9"}],
    "auth": {"type": "basic"},
}
USERS = {"users": {"alice": "a long live-test password"}}
# A domain with no DNS provider, and a route on it that would need its own certificate otherwise.
PLAIN_DOMAIN = {"zone": "example.net"}
PLAIN_ROUTE = {
    "version": 1,
    "name": "www",
    "type": "redirect",
    "hosts": ["www.example.net"],
    "exposure": "lan",
    "redirect": {"to": "http://example.net/"},
}

# A job's answer, a TLS handshake or a JSON body that is not there yet.
NOT_YET = (OSError, ValueError, x509.ExtensionNotFound)


@pytest.fixture(scope="module")
def admin(test_name_prefix: str) -> Iterator[tuple[Server, str]]:
    """A set-up container in self-signed mode and a session cookie for it."""
    settings = ("HOMEPROXY_DEV_SELF_SIGNED=1", "CLOUDFLARE_BASE_URL=http://127.0.0.1:9")
    with start_server(f"{test_name_prefix}-apply", *settings) as server:
        finish_setup(server)
        login = server.call("POST", "/api/v1/auth/login", json={"password": PASSWORD})
        assert login.status_code == 204
        yield server, session_cookie(login)


def served_names(address: str, host: str) -> list[str]:
    """The DNS names of the certificate HAProxy serves for `host` on port 443."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE  # only the names are read
    with (
        socket.create_connection((address, 443), timeout=5) as raw,
        context.wrap_socket(raw, server_hostname=host) as tls,
    ):
        der = tls.getpeercert(binary_form=True)
    assert der is not None
    names = x509.load_der_x509_certificate(der).extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    )
    return list(names.value.get_values_for_type(x509.DNSName))


def check_status_certificates_jobs_and_audit(
    server: Server, cookie: str, applied: dict[str, Any]
) -> None:
    """After the first apply: the status reads the live HAProxy and DNS server, the issuance job
    finished, the certificate is listed and renews as a job, the apply is audited and the log
    collector runs."""

    def get(path: str) -> Any:
        response = server.call("GET", f"/api/v1{path}", cookie)
        assert response.status_code == 200, response.text
        return response.json()

    def finished(job_id: str) -> bool:
        job = get(f"/jobs/{job_id}")
        assert job["state"] != "failed", job
        return bool(job["state"] == "finished")

    status = get("/status")
    assert status["haproxy_version"].startswith("3.4"), status
    assert status["haproxy_uptime_seconds"] >= 0
    assert status["routes"] == [
        {"name": "app", "status": "DOWN", "requests": 0, "responses_5xx": 0}
    ]
    assert status["last_apply"]["snapshot_id"] == applied["snapshot_id"]
    # The DNS process binds the applied address after its SIGHUP, so it may not be up yet.
    wait_until(
        lambda: bool(get("/status")["dns_listening"]),
        "DNS to listen",
        timeout=TIMEOUT,
        interval=0.5,
        not_yet=NOT_YET,
    )
    dns_uptime = get("/status")["dns_uptime_seconds"]
    assert isinstance(dns_uptime, int) and dns_uptime >= 0, dns_uptime
    wait_until(
        lambda: finished(applied["certificate_jobs"][0]),
        "the certificate job",
        timeout=TIMEOUT,
        interval=0.5,
        not_yet=NOT_YET,
    )
    assert [c["name"] for c in get("/certificates")] == ["example.com"]
    renew = server.call("POST", "/api/v1/certificates/example.com/renew", cookie)
    assert renew.status_code == 202, renew.text
    wait_until(
        lambda: finished(renew.json()["id"]),
        "the renewal job",
        timeout=TIMEOUT,
        interval=0.5,
        not_yet=NOT_YET,
    )
    assert get("/audit")[0]["action"] == "apply"
    assert "syslog.sock" in docker("exec", server.name, "ls", "/run/homeproxy")


def test_a_domain_and_route_apply_then_roll_back_through_history(
    admin: tuple[Server, str],
) -> None:
    """Add a domain and a basic auth route, and a domain with no certificate with a route. Review,
    apply, get the wildcard certificate and none for the other domain. A discarded rename still
    applies. Remove both, which removes the certificate and the users, and
    restore the first snapshot into pending changes. With the Cloudflare integration on, a
    Cloudflare that cannot be reached fails only the reconcile job, not the apply, and the new
    domain gets a DDNS result at once. A public allowlist range is an apply warning."""
    server, cookie = admin

    def call(method: str, path: str, **kwargs: Any) -> Any:
        # An apply runs `haproxy -c`, a reload and the first health checks. Under load that
        # passes httpx's 5 s default, and the dashboard sets no timeout of its own.
        response = server.call(method, f"/api/v1{path}", cookie, timeout=TIMEOUT, **kwargs)
        assert response.status_code < 300, response.text
        return response.json() if response.content else None

    call("POST", "/domains", json=DOMAIN)
    call("POST", "/domains", json=PLAIN_DOMAIN)
    call("POST", "/routes", json=ROUTE)
    call("POST", "/routes", json=PLAIN_ROUTE)
    call("PUT", "/routes/app/basic-auth", json=USERS)
    settings = call("GET", "/settings") | {"cloudflare": {"enabled": True}}
    settings["crowdsec"]["allowlist"] = ["0.0.0.0/1"]
    settings["network"]["public_ip"] = "192.0.2.7"  # the DDNS run must not ask the internet
    call("PUT", "/settings", json=settings)
    review = call("GET", "/changes")
    assert [c["path"] for c in review["changes"]] == [
        "config.yaml",
        "routes/app.yaml",
        "routes/www.yaml",
    ]
    assert "$argon2" not in review["config_diff"]

    first = call("POST", "/apply")

    assert (first["success"], first["stage"]) == (True, "done"), first
    assert any("127.0.0.1:9 is down" in warning for warning in first["warnings"])
    assert any("allowlist: 0.0.0.0/1 is a public range" in w for w in first["warnings"])
    job = f"/api/v1/jobs/{first['cloudflare_job']}"
    wait_until(
        lambda: server.call("GET", job, cookie).json()["state"] == "failed",
        "the job to fail",
        timeout=TIMEOUT,
        interval=0.5,
        not_yet=NOT_YET,
    )
    assert CLOUDFLARE_DOWN in server.call("GET", job, cookie).json()["error"]
    # The DDNS run at start saw no domain, and the timer waits 5 minutes.
    wait_until(
        lambda: bool(call("GET", "/cloudflare/records")["ddns"]),
        "the DDNS record",
        timeout=TIMEOUT,
        interval=0.5,
        not_yet=NOT_YET,
    )
    records = call("GET", "/cloudflare/records")
    assert [(r["zone"], r["action"]) for r in records["ddns"]] == [("example.com", "failed")]
    assert records["ddns_checked_at"] is not None
    # Only example.com's wildcard: the domain with no provider and its route get none.
    assert len(first["certificate_jobs"]) == 1
    wait_until(
        lambda: "*.example.com" in served_names(server.ip, "app.example.com"),
        "the wildcard certificate to be served",
        timeout=TIMEOUT,
        interval=0.5,
        not_yet=NOT_YET,
    )
    check_status_certificates_jobs_and_audit(server, cookie, first)

    call("PUT", "/routes/app", json=ROUTE | {"name": "web"})
    call("DELETE", "/changes")
    resync = call("POST", "/apply")
    assert resync["success"], resync

    call("DELETE", "/routes/app")
    call("DELETE", "/domains/example.com")
    second = call("POST", "/apply")

    assert second["success"], second
    assert "example.com.pem" not in docker("exec", server.name, "ls", "/data/certs")
    secrets = docker("exec", server.name, "cat", "/data/secrets/secrets.yaml")
    assert not parse_tree(secrets.encode(), "secrets.yaml").get("basic_auth")
    snapshots = call("GET", "/history")
    # The oldest snapshot is from setup's first apply.
    assert [s["id"] for s in snapshots][:2] == [second["snapshot_id"], first["snapshot_id"]]
    restored = call("POST", f"/history/{first['snapshot_id']}/restore")
    assert {"path": "routes/app.yaml", "kind": "write"} in restored["changes"]
    assert TOKEN not in server.logs()
