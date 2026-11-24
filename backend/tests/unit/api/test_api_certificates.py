"""`/certificates` and `/jobs` on the real app: the installed list, and a renewal as a job."""

from pathlib import Path

from fastapi.testclient import TestClient

from homeproxy.certs.selfsigned import self_signed_bundle


def test_installed_certificates_are_listed_with_their_expiry(lab: Path, api: TestClient) -> None:
    """Each bundle in `/data/certs` is listed, except the default certificate."""
    (lab / "certs").mkdir()
    (lab / "certs" / "example.com.pem").write_bytes(
        self_signed_bundle(("example.com", "*.example.com"))
    )
    (lab / "certs" / "default.pem").write_bytes(self_signed_bundle(("default",)))

    listed = api.get("/api/v1/certificates").json()

    assert [(c["name"], c["names"], c["state"]) for c in listed] == [
        ("example.com", ["example.com", "*.example.com"], "ok")
    ]
    assert listed[0]["days_left"] > 21


def test_planned_certificates_are_the_wildcard_and_hosts_outside_it(
    lab: Path, api: TestClient
) -> None:
    """app.example.com is under the wildcard; a host two labels down needs its own."""
    (lab / "config" / "routes" / "deep.yaml").write_text(
        "version: 1\nname: deep\ntype: http\nhosts: [a.b.example.com]\nexposure: lan\n"
        "upstreams: [{address: 192.168.1.10:80}]\n"
    )

    planned = api.get("/api/v1/certificates/planned").json()

    assert planned == [
        {"name": "a.b.example.com", "zone": "example.com"},
        {"name": "example.com", "zone": "example.com"},
    ]


def test_a_renewal_returns_a_job_that_jobs_report(lab: Path, api: TestClient) -> None:
    """Renewing a certificate the config needs answers 202 with a queued issuance job."""
    response = api.post("/api/v1/certificates/example.com/renew")

    assert response.status_code == 202
    job = response.json()
    assert (job["kind"], job["subject"], job["state"]) == (
        "certificate.issue",
        "example.com",
        "queued",
    )
    assert api.get(f"/api/v1/jobs/{job['id']}").json() == job
    assert api.get("/api/v1/jobs").json() == [job]


def test_unknown_certificates_and_jobs_are_not_found(lab: Path, api: TestClient) -> None:
    """A name the config needs no certificate for starts no job, and an unknown job is 404."""
    renew = api.post("/api/v1/certificates/other.example.net/renew")
    job = api.get("/api/v1/jobs/no-such-job")

    assert (renew.status_code, renew.json()["error"]["code"]) == (404, "certificate_not_found")
    assert (job.status_code, job.json()["error"]["code"]) == (404, "job_not_found")
    assert api.get("/api/v1/jobs").json() == []
