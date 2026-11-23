"""The Cloudflare range refresh, with a local stand-in for cloudflare.com.

`tests/integration/real_ip/` covers a refresh that reaches a real HAProxy.
"""

import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from homeproxy.generator.cloudflare_ranges import CACHE_FILE, CLOUDFLARE_LIST, RangesError
from homeproxy.haproxy.client import HAProxyClient, HAProxyConnectionError
from homeproxy.jobs import JobRunner, JobState
from homeproxy.jobs.cloudflare_ranges import KIND, RANGES_URL_ENV, refresh, schedule_refresh
from homeproxy.store import Store
from tests.conftest import wait_until

LISTS = {"/ips-v4": b"173.245.48.0/20\n", "/ips-v6": b"2400:cb00::/32\n"}
SAVED = b"173.245.48.0/20\n2400:cb00::/32\n"


class Cloudflare(ThreadingHTTPServer):
    """Serves `pages` by path, and 404 for any other path."""

    pages: dict[str, bytes]


class Handler(BaseHTTPRequestHandler):
    server: Cloudflare

    def do_GET(self) -> None:
        if self.path not in self.server.pages:
            self.send_error(404)
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(self.server.pages[self.path])

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def cloudflare() -> Iterator[Cloudflare]:
    server = Cloudflare(("127.0.0.1", 0), Handler)
    server.pages = dict(LISTS)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def urls(server: Cloudflare) -> list[str]:
    host, port = server.server_address[:2]
    return [f"http://{host!s}:{port}{path}" for path in LISTS]


@pytest.fixture
def unreachable(tmp_path: Path) -> HAProxyClient:
    """A client whose sockets do not exist, as when HAProxy is down."""
    missing = str(tmp_path / "missing.sock")
    return HAProxyClient(missing, missing)


@pytest.mark.parametrize(
    ("path", "body", "error"),
    [
        ("/ips-v6", None, "ips-v6 answered HTTP 404"),
        ("/ips-v4", b"173.245.48.0/20\n" * 5000, "ips-v4 is larger than"),
        ("/ips-v4", b"<html>captive portal</html>\n", "ips-v4: line 1"),
    ],
    ids=["missing", "too-large", "not-a-list"],
)
def test_failed_fetch_changes_nothing(
    tmp_path: Path,
    cloudflare: Cloudflare,
    unreachable: HAProxyClient,
    path: str,
    body: bytes | None,
    error: str,
) -> None:
    if body is None:
        del cloudflare.pages[path]
    else:
        cloudflare.pages[path] = body

    with pytest.raises(RangesError, match=error):
        refresh(Store(tmp_path), unreachable, urls(cloudflare))

    assert not (tmp_path / CACHE_FILE).exists()


def test_haproxy_that_does_not_take_the_list_keeps_the_live_file_and_the_next_run_retries(
    tmp_path: Path, cloudflare: Cloudflare, unreachable: HAProxyClient
) -> None:
    live = tmp_path / "releases" / "live"
    live.mkdir(parents=True)
    (live / CLOUDFLARE_LIST).write_bytes(b"103.21.244.0/22\n")
    (tmp_path / "generated").symlink_to("releases/live")

    for _ in range(2):
        with pytest.raises(HAProxyConnectionError):
            refresh(Store(tmp_path), unreachable, urls(cloudflare))
        # The live file still matches what HAProxy runs, so the next run tries again.
        assert (live / CLOUDFLARE_LIST).read_bytes() == b"103.21.244.0/22\n"

    # The next apply renders the new list.
    assert (tmp_path / CACHE_FILE).read_bytes() == SAVED


def finished(runner: JobRunner) -> bool:
    return any(job.state is JobState.FINISHED for job in runner.tracker.jobs())


def test_schedule_refreshes_at_once_from_the_url_in_the_environment(
    tmp_path: Path,
    cloudflare: Cloudflare,
    unreachable: HAProxyClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The app schedules the refresh with no URLs, so the environment decides where it fetches."""
    host, port = cloudflare.server_address[:2]
    monkeypatch.setenv(RANGES_URL_ENV, f"http://{host!s}:{port}/")
    runner = JobRunner(max_workers=1)
    try:
        schedule_refresh(runner, Store(tmp_path), unreachable)
        runner.start()
        wait_until(lambda: finished(runner), "the refresh job", timeout=10, interval=0.01)
        assert [(job.kind, job.state) for job in runner.tracker.jobs()] == [
            (KIND, JobState.FINISHED)
        ]
        # Before the first apply there is no live list, so HAProxy is not called.
        assert (tmp_path / CACHE_FILE).read_bytes() == SAVED
    finally:
        runner.shutdown(wait=True)
