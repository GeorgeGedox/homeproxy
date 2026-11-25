"""`GET /metrics/series`: request counts from the access logs, for the Metrics page charts.

`collect_metrics` connects the log collector to the metrics file for the life of the API.
"""

import asyncio
import logging
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request
from starlette.concurrency import run_in_threadpool

from homeproxy.api.errors import ApiError
from homeproxy.logs import LogCollector
from homeproxy.logs.metrics import Group, MetricsStore, SeriesPoint

router = APIRouter(prefix="/api/v1", tags=["metrics"])

logger = logging.getLogger(__name__)

Range = Literal["1h", "6h", "24h", "7d", "30d", "90d"]
_RANGES: dict[Range, timedelta] = {
    "1h": timedelta(hours=1),
    "6h": timedelta(hours=6),
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
    "90d": timedelta(days=90),
}


def open_metrics(path: Path) -> MetricsStore | None:
    """Open the metrics file, replacing it when it is corrupt. Return None if that fails too.

    The file is disposable, so losing the charts beats an API that does not start.
    """
    try:
        try:
            return MetricsStore(path)
        except sqlite3.DatabaseError as error:
            logger.error("metrics file %s is unreadable, starting a new one: %s", path, error)
        # A stale WAL would be replayed into the new file.
        for suffix in ("", "-wal", "-shm"):
            path.with_name(path.name + suffix).unlink(missing_ok=True)
        return MetricsStore(path)
    except (OSError, sqlite3.Error) as error:
        logger.error("cannot open the metrics file, charts stay empty: %s", error)
        return None


@asynccontextmanager
async def collect_metrics(
    collector: LogCollector, path: Path
) -> AsyncIterator[MetricsStore | None]:
    """Count the collector's lines into the file at `path` until exit, then save the last counts.

    Yields the store, or None when the file cannot be opened.
    """
    store = await run_in_threadpool(open_metrics, path)
    if store is None:
        yield None
        return
    unsubscribe = collector.subscribe(store.record)
    saving = asyncio.create_task(store.run())
    try:
        yield store
    finally:
        unsubscribe()
        saving.cancel()
        with suppress(asyncio.CancelledError):
            await saving
        # A task cancelled before its first step never runs its own final save.
        store.save()


@router.get(
    "/metrics/series",
    response_model=list[SeriesPoint],
    responses={503: {"description": "The metrics file could not be opened"}},
)
def series(
    request: Request,
    metric: Literal["requests"] = "requests",
    period: Annotated[Range, Query(alias="range")] = "24h",
    group: Group | None = None,
) -> list[SeriesPoint]:
    """Return the `metric` over `range`, oldest first, split by `group` when it is set.

    The only metric is the request count. Ranges up to 24h have one point per minute, longer
    ones one per hour. A bucket with no requests has no point. `group` is None on a point whose
    line had no value, such as the status of a TCP line.
    """
    store: MetricsStore | None = request.app.state.metrics
    if store is None:
        raise ApiError(503, "metrics_unavailable", "The metrics file could not be opened.")
    now = datetime.now(UTC)
    return store.series(now - _RANGES[period], now, group)
