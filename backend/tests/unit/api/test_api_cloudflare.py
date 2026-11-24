"""`/cloudflare` on the real app, against the fake Cloudflare and the seeded example data.

The seeded routes want CNAMEs for `jellyfin` and `whoami` in example.com, and for `whoami` and
the passthrough `git` in example.net. The record rules themselves are tested in
`tests/unit/cloudflare`; these tests check that the endpoints reach them.
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from homeproxy.store import Store
from tests.unit.api.conftest import make_client
from tests.unit.cloudflare.conftest import FakeCloudflare, cloudflare, store

__all__ = ["cloudflare", "store"]  # fixtures, imported so pytest finds them in this module

DDNS_TAG = "managed-by:homeproxy ddns:example.com"
EXPECTED_RECORDS = [
    "jellyfin.example.com",
    "whoami.example.com",
    "whoami.example.net",
    "git.example.net",
]


@pytest.fixture
def api(store: Store) -> TestClient:
    """A signed-in client on the seeded data, which is applied and has the integration on."""
    client = make_client(store.root)
    assert client.post("/api/v1/auth/login", json={"password": "homeproxy-dev"}).status_code == 204
    return client


def names(changes: list[dict[str, Any]]) -> list[tuple[str, str]]:
    return [(change["action"], change["name"]) for change in changes]


def test_records_show_owned_records_and_the_pending_plan(
    cloudflare: FakeCloudflare, api: TestClient
) -> None:
    """A dry run of the pending config: a staged route removal drops its creates, a hand-made
    record is a conflict, and the owned DDNS record is listed. Nothing is written."""
    cloudflare.add("example.com", "jellyfin.example.com", content="192.0.2.9", type="A")
    ddns = cloudflare.add(
        "example.com", "home.example.com", DDNS_TAG, content="192.0.2.7", type="A"
    )
    assert api.delete("/api/v1/routes/whoami").status_code == 204

    body = api.get("/api/v1/cloudflare/records").json()

    assert (body["enabled"], body["last_reconcile"], body["ddns"], body["ddns_checked_at"]) == (
        True,
        None,
        [],
        None,
    )
    com, net = body["zones"]
    assert [record["id"] for record in com["records"]] == [ddns]
    assert (com["changes"], com["conflicts"]) == (
        [],
        [{"name": "jellyfin.example.com", "route": "jellyfin"}],
    )
    assert names(net["changes"]) == [("create", "git.example.net")]
    assert cloudflare.writes() == []


def test_reconcile_now_then_purge_keeps_the_ddns_record(
    cloudflare: FakeCloudflare, api: TestClient
) -> None:
    """Reconcile creates the route records, is shown as the last run and queues a DDNS run.
    Purge removes them, leaves the DDNS record and is audited with the names it deleted."""
    ddns = cloudflare.add(
        "example.com", "home.example.com", DDNS_TAG, content="192.0.2.7", type="A"
    )

    run = api.post("/api/v1/cloudflare/reconcile")

    assert run.status_code == 200, run.text
    assert [len(zone["changes"]) for zone in run.json()["zones"]] == [2, 2]
    assert api.get("/api/v1/cloudflare/records").json()["last_reconcile"] == run.json()
    assert [job["kind"] for job in api.get("/api/v1/jobs").json()] == ["cloudflare.ddns"]
    purged = api.post("/api/v1/cloudflare/purge").json()
    assert names(purged[0]["changes"]) == [
        ("delete", "jellyfin.example.com"),
        ("delete", "whoami.example.com"),
    ]
    assert list(cloudflare.zone("example.com")) == [ddns]
    assert cloudflare.zone("example.net") == {}
    audit = api.get("/api/v1/audit").json()[0]
    assert (audit["action"], audit["details"]["errors"]) == ("cloudflare.purge", [])
    assert sorted(audit["details"]["deleted"]) == sorted(EXPECTED_RECORDS)


def test_reconcile_refuses_while_config_has_hand_edits(
    cloudflare: FakeCloudflare, store: Store, api: TestClient
) -> None:
    """A hand edit that is not applied yet makes reconcile answer 409 and call nothing."""
    with store.config_path.open("a") as config:
        config.write("# a hand edit\n")

    refused = api.post("/api/v1/cloudflare/reconcile")

    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "reconcile_refused"
    assert cloudflare.calls == []
    last = api.get("/api/v1/cloudflare/records").json()["last_reconcile"]
    assert "not applied yet" in last["error"]


def test_adopt_marks_the_hand_made_record_and_queues_a_ddns_run(
    cloudflare: FakeCloudflare, api: TestClient
) -> None:
    """Adopt changes only the record's comment, is audited, and queues the DDNS run that moves
    the address."""
    record = cloudflare.add("example.com", "home.example.com", content="192.0.2.50", type="A")

    adopted = api.post("/api/v1/cloudflare/ddns/example.com/adopt")

    assert adopted.status_code == 200, adopted.text
    assert adopted.json()["action"] == "adopted"
    assert cloudflare.zone("example.com")[record]["comment"] == DDNS_TAG
    assert [job["kind"] for job in api.get("/api/v1/jobs").json()] == ["cloudflare.ddns"]
    audit = api.get("/api/v1/audit").json()[0]
    assert (audit["action"], audit["details"]) == (
        "cloudflare.adopt",
        {"zone": "example.com", "record": "home.example.com"},
    )


@pytest.mark.parametrize(
    ("zone", "broken", "status", "code"),
    [
        pytest.param("example.org", False, 409, "adopt_refused", id="unknown-zone"),
        pytest.param("example.com", True, 502, "cloudflare_error", id="cloudflare-fails"),
    ],
)
def test_adopt_errors(
    cloudflare: FakeCloudflare, api: TestClient, zone: str, broken: bool, status: int, code: str
) -> None:
    """A refused adoption and a failed Cloudflare call answer in the error shape, with no run."""
    if broken:
        cloudflare.broken["GET /zones"] = (500, {}, b"{}")

    response = api.post(f"/api/v1/cloudflare/ddns/{zone}/adopt")

    assert (response.status_code, response.json()["error"]["code"]) == (status, code)
    assert api.get("/api/v1/jobs").json() == []
