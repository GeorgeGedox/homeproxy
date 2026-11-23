"""Request counters from the access logs, kept in SQLite so charts survive a restart.

Subscribe `MetricsStore.record` to the log collector. It counts each line in memory by minute,
route, country and status. `flush` adds the counts to two tables: per-minute rows, kept for
48 hours, and per-hour rows, kept for 90 days. Both tables are updated on each flush, so the
hourly rollup needs no separate job and always includes the current hour.
"""

import asyncio
import logging
import sqlite3
from collections import Counter
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from homeproxy.logs.parser import AccessLog

MINUTE_RETENTION = timedelta(hours=48)
HOUR_RETENTION = timedelta(days=90)
FLUSH_INTERVAL = 10.0
"""Seconds between writes to SQLite. A crash loses at most this much."""

Group = Literal["route", "country", "status"]

logger = logging.getLogger(__name__)

# Table and column names come only from these fixed maps, never from a caller's string.
_TABLES = {60: "minute", 3600: "hour"}
_GROUP_COLUMNS: dict[Group | None, str] = {
    None: "NULL",
    "route": "NULLIF(route, '')",
    "country": "NULLIF(country, '')",
    "status": "NULLIF(status, 0)",
}
_SCHEMA = """
CREATE TABLE IF NOT EXISTS {table} (
    bucket INTEGER NOT NULL,
    route TEXT NOT NULL,
    country TEXT NOT NULL,
    status INTEGER NOT NULL,
    requests INTEGER NOT NULL,
    PRIMARY KEY (bucket, route, country, status)
) WITHOUT ROWID
"""
_UPSERT = """
INSERT INTO {table} VALUES (?, ?, ?, ?, ?)
ON CONFLICT (bucket, route, country, status) DO UPDATE SET requests = requests + excluded.requests
"""
_SERIES = """
SELECT bucket, {column}, SUM(requests) FROM {table}
WHERE bucket >= ? AND bucket < ? GROUP BY bucket, 2 ORDER BY bucket
"""

# Minute bucket (Unix seconds), route, country, status. A missing value is "" or 0, because a
# NULL in a primary key would never conflict and the counts would not add up.
_Key = tuple[int, str, str, int]


@dataclass(frozen=True, slots=True)
class SeriesPoint:
    """Requests in one bucket that starts at `at`.

    `group` is the route, country code or status the point counts, or None when the series is
    not grouped or the line had no value, such as a TCP line's status.
    """

    at: datetime
    group: str | int | None
    requests: int


class MetricsStore:
    """Count access lines and keep the counts in a SQLite file (WAL mode)."""

    def __init__(self, path: Path) -> None:
        """Open the database at `path` and create its tables. The file is disposable."""
        self.path = path
        self._pending: Counter[_Key] = Counter()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            for table in _TABLES.values():
                db.execute(_SCHEMA.format(table=table))

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """A connection per call, so a query may run in another thread."""
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute("PRAGMA synchronous=NORMAL")
            yield db

    def record(self, entry: AccessLog) -> None:
        """Count one access line in memory. A collector listener: it never touches the disk."""
        minute = int(entry.logged_at.timestamp()) // 60 * 60
        self._pending[(minute, entry.route or "", entry.country or "", entry.status or 0)] += 1

    def flush(self) -> None:
        """Add the counts held in memory to the minute and hour tables.

        If the write fails, the counts are dropped rather than kept to grow without limit.
        """
        if not self._pending:
            return
        pending, self._pending = self._pending, Counter()
        with self._connect() as db:
            for step, table in _TABLES.items():
                db.executemany(
                    _UPSERT.format(table=table),
                    ((key[0] // step * step, *key[1:], count) for key, count in pending.items()),
                )

    def prune(self, now: datetime) -> None:
        """Delete minute rows older than `MINUTE_RETENTION` and hour rows older than
        `HOUR_RETENTION`."""
        with self._connect() as db:
            for table, retention in (("minute", MINUTE_RETENTION), ("hour", HOUR_RETENTION)):
                db.execute(
                    f"DELETE FROM {table} WHERE bucket < ?",  # noqa: S608 - fixed table name
                    (int((now - retention).timestamp()),),
                )

    def series(
        self, since: datetime, until: datetime, group: Group | None = None
    ) -> list[SeriesPoint]:
        """Return request counts from `since` up to `until`, oldest first.

        Buckets are minutes while `since` is within `MINUTE_RETENTION` of now, and hours before
        that. The bucket that holds `since` is included.
        """
        step = 60 if since >= datetime.now(UTC) - MINUTE_RETENTION else 3600
        sql = _SERIES.format(column=_GROUP_COLUMNS[group], table=_TABLES[step])
        start = int(since.timestamp()) // step * step
        with self._connect() as db:
            rows = db.execute(sql, (start, int(until.timestamp()))).fetchall()
        return [SeriesPoint(datetime.fromtimestamp(at, UTC), key, n) for at, key, n in rows]

    def save(self) -> None:
        """Flush and prune. A failure is logged, so the next save can try again."""
        try:
            self.flush()
            self.prune(datetime.now(UTC))
        except sqlite3.Error:
            logger.exception("could not save metrics to %s", self.path)

    async def run(self, interval: float = FLUSH_INTERVAL) -> None:
        """Save every `interval` seconds until cancelled, then save once more."""
        try:
            while True:
                await asyncio.sleep(interval)
                self.save()
        finally:
            self.save()


__all__ = [
    "FLUSH_INTERVAL",
    "HOUR_RETENTION",
    "MINUTE_RETENTION",
    "Group",
    "MetricsStore",
    "SeriesPoint",
]
