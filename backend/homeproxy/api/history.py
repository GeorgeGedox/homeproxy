"""`GET /history`, `GET /history/{id}` and `POST /history/{id}/restore`: snapshots and rollback.

A restore stages the snapshot as pending changes. The normal Review & apply follows, and the
dashboard password never changes.
"""

from datetime import datetime
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from homeproxy.api.changes import PendingSummary, edit_lock, pending_summary
from homeproxy.api.errors import not_found
from homeproxy.auth.routes import Runtime
from homeproxy.models import ChangeSummary
from homeproxy.store.pending import PendingChanges
from homeproxy.store.snapshots import Snapshot, SnapshotNotFound, Snapshots

router = APIRouter(prefix="/api/v1", tags=["history"])

_NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"description": "No snapshot has this id"}}


class SnapshotInfo(BaseModel):
    """One snapshot: its id, when an apply took it, and what that apply changed. `message` lists
    the files, and `changes` the same in plain words, empty when the snapshot does not know them."""

    id: str
    created: datetime
    message: str
    changes: list[ChangeSummary]


class SnapshotDetail(SnapshotInfo):
    """A snapshot with the unified diff from the current config to it, which is what a restore
    would change. The password hash and secrets are redacted. Empty means no difference."""

    diff: str


def _info(snapshot: Snapshot) -> SnapshotInfo:
    return SnapshotInfo(
        id=snapshot.id,
        created=snapshot.created,
        message=snapshot.message,
        changes=list(snapshot.changes),
    )


@router.get("/history", response_model=list[SnapshotInfo])
def list_snapshots(runtime: Runtime) -> list[SnapshotInfo]:
    """Return the snapshots, newest first."""
    return [_info(snapshot) for snapshot in Snapshots(runtime.store).list()]


@router.get("/history/{snapshot_id}", response_model=SnapshotDetail, responses=_NOT_FOUND)
def get_snapshot(snapshot_id: str, runtime: Runtime) -> SnapshotDetail:
    """Return one snapshot and its diff against the current config."""
    snapshots = Snapshots(runtime.store)
    try:
        snapshot = snapshots.get(snapshot_id)
        diff = snapshots.diff(snapshot_id)
    except SnapshotNotFound:
        raise not_found("snapshot", "id") from None
    return SnapshotDetail(**_info(snapshot).model_dump(), diff=diff)


@router.post(
    "/history/{snapshot_id}/restore",
    response_model=PendingSummary,
    responses={
        **_NOT_FOUND,
        409: {"description": "An apply is running; try again"},
        422: {"description": "A domain of the snapshot names credentials that no longer exist"},
    },
)
def restore_snapshot(snapshot_id: str, runtime: Runtime) -> PendingSummary:
    """Stage a snapshot as pending changes, keeping the current password. Apply to make it live."""
    store = runtime.store
    with edit_lock(store) as held:
        try:
            Snapshots(store).restore(snapshot_id, held)
        except SnapshotNotFound:
            raise not_found("snapshot", "id") from None
        return pending_summary(PendingChanges(store).status(held))
