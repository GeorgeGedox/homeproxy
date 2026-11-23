"""Request counters in a real SQLite file: restart, rollup, retention and query speed."""

import asyncio
import sqlite3
import time
import timeit
from contextlib import closing
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path

import pytest

from homeproxy.logs.metrics import Group, MetricsStore, SeriesPoint
from homeproxy.logs.parser import AccessLog

NOW = datetime.now(UTC).replace(second=0, microsecond=0)


def line(
    at: datetime, route: str | None = "app", country: str | None = "DE", status: int | None = 200
) -> AccessLog:
    return AccessLog(
        kind="http" if status is not None else "tcp",
        logged_at=at,
        accepted_at=at,
        client_ip="192.168.1.10",
        client_port=50000,
        frontend="fe_https",
        tls=True,
        backend=f"route-{route}",
        server="192.168.1.20:8080",
        total_time_ms=3,
        status=status,
        bytes_sent=512,
        termination_state="----",
        method="GET",
        request="GET / HTTP/1.1",
        route=route,
        country=country,
        sni=None,
        line="",
    )


@pytest.mark.parametrize(
    ("group", "expected"),
    [
        (None, [(None, 4)]),
        ("route", [(None, 1), ("app", 2), ("nas", 1)]),
        ("country", [(None, 1), ("DE", 3)]),
        ("status", [(None, 1), (200, 2), (502, 1)]),
    ],
)
def test_counts_survive_a_restart(
    tmp_path: Path, group: Group | None, expected: list[tuple[str | int | None, int]]
) -> None:
    store = MetricsStore(tmp_path / "metrics.db")
    for entry in (
        line(NOW),
        line(NOW + timedelta(seconds=30)),
        line(NOW, route="nas", status=502),
        line(NOW, route=None, country=None, status=None),
    ):
        store.record(entry)
    store.flush()

    points = MetricsStore(tmp_path / "metrics.db").series(NOW, NOW + timedelta(minutes=1), group)

    assert {p.at for p in points} == {NOW}
    assert sorted(((p.group, p.requests) for p in points), key=str) == sorted(expected, key=str)


def test_stopping_the_save_loop_saves_pending_counts(tmp_path: Path) -> None:
    store = MetricsStore(tmp_path / "metrics.db")

    async def main() -> None:
        task = asyncio.create_task(store.run(interval=3600))
        store.record(line(NOW))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())

    points = MetricsStore(tmp_path / "metrics.db").series(NOW, NOW + timedelta(minutes=1))
    assert points == [SeriesPoint(NOW, None, 1)]


def test_ranges_past_48_hours_read_hourly_rows(tmp_path: Path) -> None:
    store = MetricsStore(tmp_path / "metrics.db")
    hour = NOW.replace(minute=0)
    store.record(line(hour + timedelta(minutes=1)))
    store.record(line(hour + timedelta(minutes=2)))
    store.flush()
    store.flush()

    recent = store.series(hour, NOW + timedelta(hours=1))
    older = store.series(NOW - timedelta(days=3), NOW + timedelta(hours=1))

    assert [(p.at, p.requests) for p in recent] == [
        (hour + timedelta(minutes=1), 1),
        (hour + timedelta(minutes=2), 1),
    ]
    assert older == [SeriesPoint(hour, None, 2)]


def test_prune_keeps_minutes_for_48_hours_and_hours_for_90_days(tmp_path: Path) -> None:
    path = tmp_path / "metrics.db"
    store = MetricsStore(path)
    hour = NOW.replace(minute=0)
    for age in (timedelta(hours=47), timedelta(hours=49), timedelta(days=89), timedelta(days=91)):
        store.record(line(hour - age))
    store.flush()

    store.prune(hour)

    # Old minute rows cannot be queried, so the test reads the file to see that they are gone.
    with closing(sqlite3.connect(path)) as db:
        minutes = [row[0] for row in db.execute("SELECT bucket FROM minute ORDER BY bucket")]
    assert minutes == [int((hour - timedelta(hours=47)).timestamp())]
    assert [p.at for p in store.series(hour - timedelta(days=100), hour)] == [
        hour - timedelta(days=89),
        hour - timedelta(hours=49),
        hour - timedelta(hours=47),
    ]


def test_a_90_day_query_takes_under_200_ms(tmp_path: Path) -> None:
    """Timed in CPU time of the querying thread. The query is SQLite on a file in the page cache,
    so it is CPU-bound, and on an idle machine CPU time equals wall time. Wall time would also
    count the time other processes hold the CPU, which made this test fail under load."""
    store = MetricsStore(tmp_path / "metrics.db")
    start = NOW.replace(minute=0) - timedelta(days=90)
    # A busy home lab: every hour, 10 routes, 4 countries and 4 statuses all see traffic.
    for hour in range(90 * 24):
        at = start + timedelta(hours=hour)
        for route in range(10):
            for country in ("DE", "FR", "US", None):
                for status in (200, 304, 404, 502):
                    store.record(line(at, f"route{route}", country, status))
    store.flush()

    for group in (None, "route", "country", "status"):
        assert sum(p.requests for p in store.series(start, NOW, group)) == 90 * 24 * 160
        query = partial(store.series, start, NOW, group)
        elapsed = min(timeit.repeat(query, number=1, repeat=5, timer=time.thread_time))
        assert elapsed < 0.2, f"group={group}: {elapsed:.3f} s"
