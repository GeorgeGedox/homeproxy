"""`/metrics/series` on the real app, fed by the log collector the way the API lifespan wires it."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from homeproxy.api.metrics import collect_metrics, open_metrics
from homeproxy.logs import LogCollector


def datagram(at: datetime, route: str) -> bytes:
    """An HTTP access line as HAProxy sends it to the collector."""
    stamp = at.strftime("%d/%b/%Y:%H:%M:%S.000")
    return (
        f"<134>1 {at.isoformat()} - haproxy 8 - - 192.168.1.50:60834 [{stamp}] "
        f"fe_https~ route-{route}/192.168.1.20:8080 0/0/1/2/3 200 512 - - ---- 2/1/0/0/0 0/0 "
        f'"GET / HTTP/1.1" "{route}" "DE" ""\n'
    ).encode()


def test_logged_requests_reach_the_series_after_a_restart(data_dir: Path, api: TestClient) -> None:
    """Lines the collector receives are counted, saved when the API stops, and charted by range
    and group."""
    now = datetime.now(UTC)
    collector = LogCollector(data_dir / "run" / "syslog.sock")

    async def serve() -> None:
        async with collect_metrics(collector, data_dir / "state" / "metrics.db") as store:
            assert store is not None
            for at, route in [(now, "app"), (now, "app"), (now - timedelta(hours=2), "nas")]:
                assert collector.receive(datagram(at, route)) is not None

    asyncio.run(serve())

    def series(**params: str) -> list[tuple[str | None, int]]:
        response = api.get("/api/v1/metrics/series", params=params)
        assert response.status_code == 200, response.text
        return [(point["group"], point["requests"]) for point in response.json()]

    assert series(range="1h", group="route") == [("app", 2)]
    assert sum(requests for _, requests in series(range="24h")) == 3


def test_a_corrupt_metrics_file_is_replaced(
    data_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The file is disposable: a corrupt one is logged and replaced, so the API still starts."""
    path = data_dir / "state" / "metrics.db"
    path.parent.mkdir()
    path.write_bytes(b"not a database" * 100)

    with caplog.at_level(logging.ERROR):
        store = open_metrics(path)

    assert store is not None
    assert store.series(datetime.now(UTC) - timedelta(hours=1), datetime.now(UTC)) == []
    assert "unreadable" in caplog.text


def test_series_answers_503_when_the_metrics_file_cannot_be_opened(
    data_dir: Path, api: TestClient
) -> None:
    """A metrics file that cannot be created leaves the API running without charts."""
    (data_dir / "blocked").write_text("a file where the directory should be")
    api.app.state.metrics = open_metrics(data_dir / "blocked" / "metrics.db")  # type: ignore[attr-defined]

    response = api.get("/api/v1/metrics/series")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "metrics_unavailable"


@pytest.mark.parametrize("params", [{"group": "client"}, {"range": "2y"}])
def test_unknown_group_or_range_is_refused(api: TestClient, params: dict[str, str]) -> None:
    """Only the counted groups and the listed ranges exist."""
    assert api.get("/api/v1/metrics/series", params=params).status_code == 422
