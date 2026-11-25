"""Certificate jobs run one at a time, beside the other jobs."""

import shutil
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from homeproxy.certs.lego import Lego
from homeproxy.certs.service import CertificateService
from homeproxy.haproxy.client import HAProxyClient
from homeproxy.jobs import JobRunner
from homeproxy.jobs.certs import submit_issue
from homeproxy.store import Store

DATA = Path(__file__).parents[4] / "examples" / "data"
# A lego run that lasts until the test creates the release file, then fails as a refused CA would.
SLOW_LEGO = """\
import os, sys, time
deadline = time.monotonic() + 10
while not os.path.exists({release!r}) and time.monotonic() < deadline:
    time.sleep(0.01)
sys.exit(1)
"""


@pytest.fixture
def release(tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "release"
    yield path
    path.touch()


@pytest.fixture
def service(tmp_path: Path, release: Path) -> CertificateService:
    shutil.copytree(DATA, tmp_path / "data")
    script = tmp_path / "lego"
    script.write_text(f"#!{sys.executable}\n{SLOW_LEGO.format(release=str(release))}")
    script.chmod(0o755)
    lego = Lego(tmp_path / "acme", binary=str(script))
    return CertificateService(Store(tmp_path / "data"), HAProxyClient(), lego, tmp_path / "certs")


def test_issue_jobs_waiting_for_lego_leave_workers_for_other_jobs(
    service: CertificateService, release: Path
) -> None:
    """An apply can queue one issue per certificate, and lego runs one at a time. Waiting issues
    must not hold every worker, or the heartbeat stops and the host looks down."""
    runner = JobRunner()
    runner.start()
    try:
        for _ in range(5):
            submit_issue(runner, service, "example.com")
        other = threading.Event()
        runner.submit("other", other.set)

        assert other.wait(5)
    finally:
        release.touch()
        runner.shutdown(wait=True)
