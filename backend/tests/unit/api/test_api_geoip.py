"""`/geoip` on the real app: the staged block list, the data age and credit, and the refresh job.

Blocking itself is tested against HAProxy in `tests/integration/geoip`.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from homeproxy.geoip.data import GeoipData, save_country_map
from homeproxy.jobs import JobRunner

NO_DATA = "No country can be blocked before the GeoIP data is downloaded."


def save_data(root: Path, countries: list[str]) -> None:
    """Save GeoIP data that has `countries`, as a refresh would."""
    info = GeoipData(
        provider="dbip-lite", updated_at=datetime.now(UTC), entries=1, countries=countries
    )
    save_country_map(root, "1.0.0.0/24 CN\n", info)


def test_geoip_credits_the_source_of_the_map_in_use(data_dir: Path, api: TestClient) -> None:
    """Before the first download the configured provider is credited. Once a map is saved, its
    own provider is, even when the setting names another."""
    before = api.get("/api/v1/geoip").json()
    updated = datetime(2026, 11, 1, tzinfo=UTC)
    info = GeoipData(provider="maxmind", updated_at=updated, entries=1, countries=["AU"])
    save_country_map(data_dir, "1.0.0.0/24 AU\n", info)

    after = api.get("/api/v1/geoip").json()

    assert before == {
        "provider": "dbip-lite",
        "block_countries": [],
        "data": None,
        "attribution": {"text": "IP Geolocation by DB-IP", "url": "https://db-ip.com"},
    }
    assert after["provider"] == "dbip-lite"
    assert after["data"] == {
        "provider": "maxmind",
        "updated_at": "2026-11-01T00:00:00Z",
        "entries": 1,
        "countries": ["AU"],
    }
    assert after["attribution"]["url"] == "https://www.maxmind.com"


def test_a_block_list_is_staged_until_the_next_apply(data_dir: Path, api: TestClient) -> None:
    """The GeoIP data decides which codes exist: XK is not ISO 3166, but the data has it for
    Kosovo. DU is in no data, so it is refused at its field."""
    save_data(data_dir, ["CN", "RU", "XK"])
    config = (data_dir / "config" / "config.yaml").read_text()

    staged = api.put("/api/v1/geoip", json={"block_countries": ["RU", "XK"]})
    bad = api.put("/api/v1/geoip", json={"block_countries": ["RU", "DU"]})

    assert staged.status_code == 204
    assert (bad.status_code, bad.json()["error"]) == (
        422,
        {
            "code": "unknown_country",
            "message": "DU is not a country in the GeoIP data.",
            "field": "block_countries.1",
        },
    )
    assert api.get("/api/v1/geoip").json()["block_countries"] == ["RU", "XK"]
    assert api.get("/api/v1/changes").json()["changes"] == [
        {"path": "config.yaml", "kind": "write"}
    ]
    assert (data_dir / "config" / "config.yaml").read_text() == config


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (None, f"{NO_DATA} Use Refresh data on Security → GeoIP."),
        (
            "MaxMind needs a license key in secrets.yaml",
            f"{NO_DATA} The last download failed: MaxMind needs a license key in secrets.yaml. "
            "Use Refresh data on Security → GeoIP.",
        ),
    ],
    ids=["never-downloaded", "download-failed"],
)
def test_with_no_data_a_new_code_is_refused_with_the_cause(
    api: TestClient, failure: str | None, message: str
) -> None:
    if failure is not None:
        job = api.post("/api/v1/geoip/refresh").json()
        jobs: JobRunner = api.app.state.services.jobs  # type: ignore[attr-defined]
        jobs.tracker.mark_failed(job["id"], failure)

    response = api.put("/api/v1/geoip", json={"block_countries": ["CN"]})

    assert (response.status_code, response.json()["error"]) == (
        422,
        {"code": "geoip_no_data", "message": message, "field": "block_countries.0"},
    )


def test_with_no_data_a_block_list_can_still_shrink(data_dir: Path, api: TestClient) -> None:
    """So a list saved while there was data never gets stuck."""
    with (data_dir / "config" / "config.yaml").open("a") as config:
        config.write("geoip: {block_countries: [CN, RU]}\n")

    assert api.put("/api/v1/geoip", json={"block_countries": ["RU"]}).status_code == 204
    assert api.get("/api/v1/geoip").json()["block_countries"] == ["RU"]


def test_an_unchanged_block_list_stages_nothing(api: TestClient) -> None:
    assert api.put("/api/v1/geoip", json={"block_countries": []}).status_code == 204

    assert api.get("/api/v1/changes").json()["changes"] == []


def test_a_refresh_returns_a_job_that_jobs_report(api: TestClient) -> None:
    response = api.post("/api/v1/geoip/refresh")

    assert response.status_code == 202
    job = response.json()
    assert (job["kind"], job["state"]) == ("geoip.refresh", "queued")
    assert api.get("/api/v1/jobs").json() == [job]
