"""`/dns/records` on the real app: the served table and the extra records in pending changes."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

RECORD = {"name": "nas.example.com", "target": "192.168.1.30"}


def test_the_served_table_comes_from_the_live_release(lab: Path, api: TestClient) -> None:
    """Before the first apply nothing is served. After, the release's table is returned. A table
    this release cannot read, such as one an older release wrote, shows as unknown, not a 500."""
    assert api.get("/api/v1/dns/records").json() == {"served": None, "extra": []}
    table = {
        "version": 1,
        "listen": "192.168.1.10:5335",
        "forward_to": [],
        "zones": ["example.com"],
        "records": {"app.example.com": "192.168.1.10"},
    }
    (lab / "generated").mkdir()
    (lab / "generated" / "dns-records.json").write_text(json.dumps(table))

    assert api.get("/api/v1/dns/records").json()["served"] == table
    (lab / "generated" / "dns-records.json").write_text(json.dumps(table | {"version": 0}))
    assert api.get("/api/v1/dns/records").json()["served"] is None


def test_extra_records_are_staged_then_removed(lab: Path, api: TestClient) -> None:
    """An added record is pending in `config.yaml`, and removing it stages that too."""
    created = api.post("/api/v1/dns/records", json=RECORD)

    assert created.status_code == 201, created.text
    assert api.get("/api/v1/dns/records").json()["extra"] == [RECORD]
    assert [c["path"] for c in api.get("/api/v1/changes").json()["changes"]] == ["config.yaml"]
    assert "nas.example.com" not in (lab / "config" / "config.yaml").read_text()
    assert api.delete("/api/v1/dns/records/nas.example.com").status_code == 204
    assert api.get("/api/v1/dns/records").json()["extra"] == []


@pytest.mark.parametrize(
    ("method", "path", "body", "status", "code", "message"),
    [
        pytest.param("POST", "", RECORD, 409, "record_exists", "", id="duplicate"),
        pytest.param(
            "POST",
            "",
            RECORD | {"name": "app.example.com"},
            409,
            "route_host",
            "The site app.example.com answers",
            id="route-host",
        ),
        pytest.param(
            "POST",
            "",
            RECORD | {"name": "nas.example.org"},
            422,
            "outside_domains",
            "",
            id="no-domain",
        ),
        pytest.param(
            "DELETE", "/other.example.com", None, 404, "record_not_found", "", id="unknown"
        ),
    ],
)
def test_record_edits_that_are_refused(
    lab: Path,
    api: TestClient,
    method: str,
    path: str,
    body: dict[str, str] | None,
    status: int,
    code: str,
    message: str,
) -> None:
    """A second record with the same name, a route's host, a name in no domain and an unknown
    name are refused. A route is named by its host, never its internal name."""
    assert api.post("/api/v1/dns/records", json=RECORD).status_code == 201

    response = api.request(method, f"/api/v1/dns/records{path}", json=body)

    assert (response.status_code, response.json()["error"]["code"]) == (status, code)
    assert message in response.json()["error"]["message"]
