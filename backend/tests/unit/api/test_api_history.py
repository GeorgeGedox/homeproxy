"""`/history` on the real app: list, diff and restore into pending changes."""

from pathlib import Path

from fastapi.testclient import TestClient

from homeproxy.models import ChangeSummary
from homeproxy.store import Store
from homeproxy.store.snapshots import Snapshots


def test_a_snapshot_is_listed_diffed_and_restored_into_pending(lab: Path, api: TestClient) -> None:
    """After an apply removed a route, its snapshot shows the route coming back, and a restore
    stages exactly that."""
    store = Store(lab)
    with store.lock() as held:
        added = ChangeSummary(kind="route", action="added", label="app.example.com")
        snapshot = Snapshots(store).create("add routes/app.yaml", held, [added])
    (lab / "config" / "routes" / "app.yaml").unlink()

    listed = api.get("/api/v1/history").json()
    detail = api.get(f"/api/v1/history/{snapshot.id}").json()
    restored = api.post(f"/api/v1/history/{snapshot.id}/restore").json()

    assert [(s["id"], s["message"]) for s in listed] == [(snapshot.id, "add routes/app.yaml")]
    assert listed[0]["changes"] == [added.model_dump()]
    assert "+++ b/routes/app.yaml" in detail["diff"]
    assert restored["changes"] == [{"path": "routes/app.yaml", "kind": "write"}]


def test_an_unknown_snapshot_is_not_found(lab: Path, api: TestClient) -> None:
    """An id that names no snapshot is 404 for every history call."""
    for response in (
        api.get("/api/v1/history/20260101T000000000000Z"),
        api.post("/api/v1/history/20260101T000000000000Z/restore"),
    ):
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "snapshot_not_found"
