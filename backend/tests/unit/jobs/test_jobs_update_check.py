"""The HomeProxy update check compares the running version with a release served from a file."""

import importlib.metadata
import json
from pathlib import Path

import pytest

from homeproxy.jobs import JobRunner, JobState
from homeproxy.jobs.update_check import KIND, UpdateCheck, UpdateCheckError, check_update
from tests.conftest import wait_until


def release_url(tmp_path: Path, body: object) -> str:
    path = tmp_path / "latest.json"
    path.write_text(json.dumps(body))
    return path.as_uri()


@pytest.mark.parametrize(
    ("running", "tag", "latest", "update_available"),
    [
        ("0.1.0", "v0.2.0", "0.2.0", True),
        ("0.2.0", "v0.2.0", "0.2.0", False),
        ("0.10.0", "v0.9.1", "0.9.1", False),
        ("0.1.0", "1.0.0", "1.0.0", True),
    ],
    ids=["newer", "same", "numeric-order", "no-v"],
)
def test_status_reports_a_newer_release(
    tmp_path: Path, running: str, tag: str, latest: str, update_available: bool
) -> None:
    status = check_update(running, release_url(tmp_path, {"tag_name": tag, "html_url": "x"}))
    assert (status.running, status.latest, status.update_available) == (
        running,
        latest,
        update_available,
    )


@pytest.mark.parametrize(
    "body",
    [None, "<html>not json</html>", {"name": "v0.2.0"}, {"tag_name": "v0.2.0-rc1"}],
    ids=["missing", "not-json", "no-tag", "not-x.y.z"],
)
def test_failed_check_keeps_the_previous_status(tmp_path: Path, body: object) -> None:
    check = UpdateCheck(release_url(tmp_path, {"tag_name": "v99.0.0"}))
    check.run()
    previous = check.status
    assert previous is not None
    assert previous.running == importlib.metadata.version("homeproxy")
    path = tmp_path / "latest.json"
    if body is None:
        path.unlink()
    else:
        path.write_text(body if isinstance(body, str) else json.dumps(body))
    with pytest.raises(UpdateCheckError):
        check.run()
    assert check.status is previous


def test_an_answer_cut_short_is_an_update_check_error(cut_short_url: str) -> None:
    with pytest.raises(UpdateCheckError):
        check_update("0.1.0", cut_short_url)


def jobs(runner: JobRunner) -> list[tuple[str, JobState]]:
    return [(job.kind, job.state) for job in runner.tracker.jobs()]


@pytest.mark.parametrize("configured", [True, False])
def test_schedule_runs_the_check_only_with_a_source(tmp_path: Path, configured: bool) -> None:
    """Without a source nothing is fetched, and no job runs that could fail."""
    url = release_url(tmp_path, {"tag_name": "v99.0.0"}) if configured else None
    check = UpdateCheck(url)
    runner = JobRunner(max_workers=1)
    try:
        check.schedule(runner)
        runner.start()
        if configured:
            done = [(KIND, JobState.FINISHED)]
            wait_until(lambda: jobs(runner) == done, "the check job", timeout=10, interval=0.01)
        assert jobs(runner) == ([(KIND, JobState.FINISHED)] if configured else [])
        assert (check.status is not None) is configured
    finally:
        runner.shutdown(wait=True)
