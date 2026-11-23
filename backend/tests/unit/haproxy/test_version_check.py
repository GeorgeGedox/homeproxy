"""The version check reports a newer LTS patch, using a local stand-in for endoflife.date."""

import json
import threading
from collections.abc import Iterator
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from homeproxy.haproxy.version_check import VersionCheckError
from homeproxy.jobs import JobRunner, JobState
from homeproxy.jobs.version_check import KIND, VersionCheck
from tests.conftest import wait_until


def release(name: str, latest: str, *, lts: bool, eol: str | None) -> dict[str, object]:
    """One release branch in the shape of the endoflife.date v1 API."""
    return {
        "name": name,
        "label": name,
        "isLts": lts,
        "isEol": False,
        "eolFrom": eol,
        "latest": {"name": latest, "date": "2026-10-14", "link": None},
    }


# The newest branch is not LTS, and an older LTS branch has a higher patch number.
RELEASES = {
    "schema_version": "1.2.1",
    "result": {
        "name": "haproxy",
        "releases": [
            release("3.5", "3.5.2", lts=False, eol="2027-07-01"),
            release("3.4", "3.4.9", lts=True, eol="2031-04-01"),
            release("3.2", "3.2.26", lts=True, eol="2030-04-01"),
        ],
    },
}


class EndOfLife(ThreadingHTTPServer):
    """Serves `body` with `status` for every GET."""

    status = 200
    body = json.dumps(RELEASES).encode()
    cut_short = False


class Handler(BaseHTTPRequestHandler):
    server: EndOfLife

    def do_GET(self) -> None:
        self.send_response(self.server.status)
        self.send_header("Content-Type", "application/json")
        extra = 100 if self.server.cut_short else 0
        self.send_header("Content-Length", str(len(self.server.body) + extra))
        self.end_headers()
        self.wfile.write(self.server.body)

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def endoflife() -> Iterator[EndOfLife]:
    server = EndOfLife(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def url(server: EndOfLife) -> str:
    host, port = server.server_address[:2]
    return f"http://{host!s}:{port}/api/v1/products/haproxy/"


@pytest.mark.parametrize(
    ("running", "update_available"),
    [
        ("3.4.7-8872a26", True),
        ("3.4.9-1a2b3c4", False),
        ("3.6-dev1", False),
    ],
)
def test_status_reports_newer_patch_of_newest_lts(
    endoflife: EndOfLife, running: str, update_available: bool
) -> None:
    check = VersionCheck(lambda: running, url(endoflife))
    check.run()
    assert check.status is not None
    assert check.status.running == running
    assert check.status.branch == "3.4"
    assert check.status.latest == "3.4.9"
    assert check.status.eol == date(2031, 4, 1)
    assert check.status.update_available is update_available


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (500, b"oops"),
        (200, b"<html>not json</html>"),
        (200, json.dumps({"result": {"releases": [{"name": "3.4"}]}}).encode()),
        (200, json.dumps({"result": {"releases": []}}).encode()),
        (200, json.dumps(RELEASES).replace("3.4.9", "latest").encode()),
        (200, None),
    ],
    ids=["http-error", "not-json", "missing-fields", "no-lts", "bad-version", "cut-short"],
)
def test_failed_check_keeps_the_previous_status(
    endoflife: EndOfLife, status: int, body: bytes | None
) -> None:
    check = VersionCheck(lambda: "3.4.7", url(endoflife))
    check.run()
    previous = check.status
    endoflife.status, endoflife.cut_short = status, body is None
    endoflife.body = endoflife.body if body is None else body
    with pytest.raises(VersionCheckError):
        check.run()
    assert check.status is previous


def test_unreachable_endoflife_is_a_version_check_error(endoflife: EndOfLife) -> None:
    check = VersionCheck(lambda: "3.4.7", url(endoflife))
    endoflife.shutdown()
    endoflife.server_close()
    with pytest.raises(VersionCheckError):
        check.run()


def finished(runner: JobRunner) -> bool:
    return any(job.state is JobState.FINISHED for job in runner.tracker.jobs())


def test_schedule_runs_the_check_at_once_as_a_job(endoflife: EndOfLife) -> None:
    check = VersionCheck(lambda: "3.4.7", url(endoflife))
    runner = JobRunner(max_workers=1)
    try:
        check.schedule(runner)
        runner.start()
        wait_until(lambda: finished(runner), "the check job", timeout=10, interval=0.01)
        assert [(job.kind, job.state) for job in runner.tracker.jobs()] == [
            (KIND, JobState.FINISHED)
        ]
        assert check.status is not None
        assert check.status.update_available
    finally:
        runner.shutdown(wait=True)
