"""The save check for country block lists: the GeoIP data, not a fixed list, decides which codes
exist. The global list, a route's list and the settings all save through it."""

from pathlib import Path

from homeproxy.api.errors import ApiError
from homeproxy.geoip.data import load_data_info
from homeproxy.jobs import JobRunner
from homeproxy.jobs.geoip import KIND


def check_added_countries(
    jobs: JobRunner, root: Path, field: str, old: list[str], new: list[str]
) -> None:
    """Answer 422 when `new` adds a code that the GeoIP data does not have, or adds any code while
    there is no data. Codes already in `old` pass, so a block list can always shrink, and an
    unrelated edit is never refused over a list saved earlier."""
    added = [(index, code) for index, code in enumerate(new) if code not in old]
    if not added:
        return
    info = load_data_info(root)
    if info is None:
        raise ApiError(422, "geoip_no_data", _no_data(jobs), field=f"{field}.{added[0][0]}")
    for index, code in added:
        if code not in info.countries:
            message = f"{code} is not a country in the GeoIP data."
            raise ApiError(422, "unknown_country", message, field=f"{field}.{index}")


def _no_data(jobs: JobRunner) -> str:
    last = next((job for job in jobs.tracker.jobs() if job.kind == KIND), None)
    failed = f" The last download failed: {last.error.rstrip('.')}." if last and last.error else ""
    start = "No country can be blocked before the GeoIP data is downloaded."
    return f"{start}{failed} Use Refresh data on Security → GeoIP."
