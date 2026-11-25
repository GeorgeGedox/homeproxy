"""Fixtures that run generated files through the Dockerfile's pinned HAProxy, in a container.

These tests need Docker. Without it they fail and do not skip, because `haproxy -c` on every golden
case is a required gate.
"""

import os
import shutil
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from homeproxy.generator import DEFAULT_CERTIFICATE_NAME, ensure_default_certificate
from homeproxy.generator.crowdsec import BOUNCER_PAGES
from homeproxy.util.run import RunResult, run
from tests.golden.live_proxy import (
    NOT_THE_CLIENT,
    Proxy,
    running_proxy,
    wait_for_log,
)
from tests.integration.haproxy_live import haproxy_image as pinned_haproxy_image

# The pinned image, so a Renovate HAProxy PR runs every golden case on the new version.
HAPROXY_IMAGE = pinned_haproxy_image()
# The first run may have to pull the image.
DOCKER_TIMEOUT = 300.0


@pytest.fixture(scope="session")
def haproxy_image() -> str:
    """The image that runs generated files."""
    return HAPROXY_IMAGE


@pytest.fixture(scope="session")
def default_certificate(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One default certificate for the whole session, because creating a key takes time."""
    return ensure_default_certificate(tmp_path_factory.mktemp("certs"))


@pytest.fixture(scope="session")
def make_data_dir(
    tmp_path_factory: pytest.TempPathFactory, default_certificate: Path
) -> Callable[[], Path]:
    """Return a function that creates a directory laid out like `/data`, with the certificate."""

    def make() -> Path:
        data_dir = tmp_path_factory.mktemp("data")
        for name in ("releases", "certs", "state"):
            (data_dir / name).mkdir()
        shutil.copy(default_certificate, data_dir / "certs" / DEFAULT_CERTIFICATE_NAME)
        return data_dir

    return make


@pytest.fixture
def data_dir(make_data_dir: Callable[[], Path]) -> Path:
    """A new directory laid out like `/data`."""
    return make_data_dir()


@pytest.fixture(scope="session")
def bouncer_pages(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Stand-ins for the ban and captcha pages that the image ships with the CrowdSec bouncer."""
    pages = tmp_path_factory.mktemp("bouncer-pages")
    for name in ("ban.html", "captcha.html"):
        (pages / name).write_text(f"<html>{name}</html>\n")
    return pages


@pytest.fixture(scope="session")
def haproxy_check(bouncer_pages: Path) -> Callable[[Path, str], RunResult]:
    """Return a function that runs `haproxy -c` on `<data_dir>/<release>/conf.d/` as `/data`."""

    def check(data_dir: Path, release: str) -> RunResult:
        return run(
            [
                "docker",
                "run",
                "--rm",
                "--network=none",
                # The same user owns the files and runs HAProxy, as in the image.
                f"--user={os.getuid()}:{os.getgid()}",
                f"--volume={data_dir}:/data",
                f"--volume={bouncer_pages}:{BOUNCER_PAGES}:ro",
                HAPROXY_IMAGE,
                "haproxy",
                "-c",
                "-f",
                f"/data/{release}/conf.d/",
            ],
            timeout=DOCKER_TIMEOUT,
            check=False,
        )

    return check


@pytest.fixture(scope="module")
def outside_proxy(make_data_dir: Callable[[], Path], haproxy_image: str) -> Iterator[Proxy]:
    """HAProxy where the test client is not in the LAN."""
    with running_proxy(make_data_dir(), NOT_THE_CLIENT, haproxy_image) as proxy:
        yield proxy


@pytest.fixture(scope="module")
def lan_proxy(
    make_data_dir: Callable[[], Path], haproxy_image: str, outside_proxy: Proxy
) -> Iterator[Proxy]:
    """HAProxy where the test client is in the LAN. Its address depends on the Docker network."""
    outside_proxy.get("app.example.com")
    client = wait_for_log(outside_proxy, r"([0-9.]+):[0-9]+ \[[^]]+\] fe_https").group(1)
    with running_proxy(make_data_dir(), f"{client}/32", haproxy_image) as proxy:
        yield proxy
