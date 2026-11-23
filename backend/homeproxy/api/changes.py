"""`GET /changes` and `DELETE /changes`: the pending change set, its diffs and its conflict state.

The diffs never show a secret. The password hash in `config.yaml` is replaced, registered secrets
are redacted, and the generated userlist file, which holds the basic auth hashes, is left out.
"""

from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import APIRouter, Response
from pydantic import BaseModel

from homeproxy.api.apply import ServicesDep
from homeproxy.auth.routes import Runtime
from homeproxy.generator import USERLISTS_CONFIG, GeneratorError
from homeproxy.geoip.data import COUNTRY_MAP
from homeproxy.haproxy.change_summary import change_summary
from homeproxy.haproxy.pipeline import Pipeline, read_release
from homeproxy.models import ChangeSummary
from homeproxy.store import HeldLock, Store, StoreError
from homeproxy.store.pending import ChangeKind, PendingChanges, PendingStatus, config_files
from homeproxy.store.snapshots import diff_files

router = APIRouter(prefix="/api/v1", tags=["changes"])

LOCK_WAIT = 10.0
"""Seconds an edit waits for the store lock, for example behind an apply, before answering 409."""


@contextmanager
def edit_lock(store: Store) -> Iterator[HeldLock]:
    """Hold the store lock for an API edit. After `LOCK_WAIT` the request answers 409 `busy`."""
    with store.lock(timeout=LOCK_WAIT) as held:
        yield held


class PendingChange(BaseModel):
    """One staged file. `path` is relative to `/data/config`, such as `routes/jellyfin.yaml`."""

    path: str
    kind: ChangeKind


class PendingSummary(BaseModel):
    """The staged changes, and whether a staged file was edited by hand in `/data/config` since it
    was staged. A hand edit of any other file is not a conflict.

    While `conflict` is true, apply refuses to run. Discard the changes to go on.
    """

    changes: list[PendingChange]
    conflict: bool
    conflicting_paths: list[str]


class PendingReview(PendingSummary):
    """The change set with its summary and diffs.

    `summary` lists the changes in plain words, including basic auth users that the next apply
    loads, which have no staged file.
    `config_diff` is a unified diff of the YAML files. `generated_diff` is a unified diff of the
    HAProxy files the next apply would write, or null with `generated_error` when the pending
    config cannot be rendered.
    """

    summary: list[ChangeSummary]
    config_diff: str
    generated_diff: str | None
    generated_error: str | None


def pending_summary(status: PendingStatus) -> PendingSummary:
    """Return the API view of a pending status."""
    return PendingSummary(
        changes=[PendingChange(path=c.path, kind=c.kind) for c in status.changes],
        conflict=status.conflict,
        conflicting_paths=list(status.conflicting_paths),
    )


def _render(
    pipeline: Pipeline, pending: PendingChanges, held: HeldLock
) -> tuple[dict[str, str], dict[str, str] | None, str | None]:
    """Return the live generated files, and the files the next apply would write or the error
    that stops rendering."""
    before = read_release(pipeline.store.root / "generated")
    try:
        release = pipeline.render_overlay(pending.load_overlay(held))
    except (StoreError, GeneratorError) as error:
        return before, None, str(error)
    except FileNotFoundError:
        return before, None, "missing file: secrets.yaml"
    return before, dict(release.files), None


def _generated_diff(before: dict[str, str], after: dict[str, str]) -> str:
    """Return the diff of the generated files. The userlist is left out, because it holds the basic
    auth hashes, and the country map, because a block-list edit changes up to hundreds of thousands
    of its lines. The config diff shows that edit."""
    return diff_files(_encoded(before), _encoded(after))


def _encoded(files: dict[str, str]) -> dict[str, bytes]:
    left_out = (USERLISTS_CONFIG, COUNTRY_MAP)
    return {path: text.encode() for path, text in files.items() if path not in left_out}


@router.get("/changes", response_model=PendingReview)
def get_changes(runtime: Runtime, services: ServicesDep) -> PendingReview:
    """Return the pending changes, their summary and diffs with secrets redacted, and the conflict
    state."""
    store = runtime.store
    pending = PendingChanges(store)
    with edit_lock(store) as held:
        view = pending_summary(pending.status(held))
        live = {path: file.read_bytes() for path, file in config_files(store).items()}
        staged = pending.overlay_files(held)
        before, after, generated_error = _render(services.pipeline, pending, held)
        userlists = None
        if after is not None:
            userlists = (before.get(USERLISTS_CONFIG, ""), after.get(USERLISTS_CONFIG, ""))
        return PendingReview(
            **view.model_dump(),
            summary=change_summary(live, staged, userlists),
            config_diff=diff_files(live, staged),
            generated_diff=None if after is None else _generated_diff(before, after),
            generated_error=generated_error,
        )


@router.delete(
    "/changes",
    status_code=204,
    response_class=Response,
    responses={409: {"description": "An apply is running; try again"}},
)
def discard_changes(runtime: Runtime) -> Response:
    """Discard every pending change. `/data/config` and HAProxy are not touched."""
    with edit_lock(runtime.store) as held:
        PendingChanges(runtime.store).discard(held)
    return Response(status_code=204)
