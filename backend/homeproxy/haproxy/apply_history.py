"""What an apply records in history: its message, its change summary and its snapshot."""

import logging
from collections.abc import Mapping

from homeproxy.generator import USERLISTS_CONFIG, RenderedRelease
from homeproxy.haproxy.change_summary import change_summary
from homeproxy.models import ChangeSummary
from homeproxy.store import HeldLock, Store
from homeproxy.store.pending import PendingChanges, config_files
from homeproxy.store.snapshots import Snapshots

logger = logging.getLogger(__name__)


def change_message(store: Store, pending: PendingChanges, held: HeldLock) -> str:
    """Return the pending edits as one line, such as `edit routes/app.yaml; add config.yaml`."""
    parts = []
    for change in pending.status(held).changes:
        exists = (store.config_dir / change.path).exists()
        verb = "delete" if change.kind == "delete" else "edit" if exists else "add"
        parts.append(f"{verb} {change.path}")
    return "; ".join(parts)


def applied_change_summary(
    store: Store,
    pending: PendingChanges,
    held: HeldLock,
    live: Mapping[str, str],
    release: RenderedRelease,
) -> list[ChangeSummary]:
    """Return the pending edits in plain words, as `GET /changes` shows them."""
    try:
        files = {path: file.read_bytes() for path, file in config_files(store).items()}
        userlists = (live.get(USERLISTS_CONFIG, ""), release.files.get(USERLISTS_CONFIG, ""))
        return change_summary(files, pending.overlay_files(held), userlists)
    except Exception as exc:
        # The summary only labels the history entry, and HAProxy already runs the release.
        logger.error("summarizing the applied edits failed: %s", exc)
        return []


def snapshot(
    store: Store,
    message: str,
    changes: list[ChangeSummary],
    promoted: bool,
    held: HeldLock,
    warnings: list[str],
) -> str | None:
    """Snapshot what this apply made live: the promoted edits or, when nothing was staged
    (`message` is empty), `/data/config` if it differs from the newest snapshot, which is
    how a hand edit goes live. A failed promote is retried later, so it takes none."""
    if message and not promoted:
        return None
    snapshots = Snapshots(store)
    try:
        if not message and snapshots.is_applied():
            return None
        if not message:
            return snapshots.create("apply saved config", held).id
        return snapshots.create(message, held, changes).id
    except OSError as exc:
        # The new config is live and promoted. Only its history entry is missing.
        logger.error("snapshot after apply failed: %s", exc)
        warnings.append("the apply succeeded, but no snapshot was saved")
        return None
