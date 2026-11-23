"""The CrowdSec bouncer lab, shared by the tests in this directory.

Starting the lab takes the longest part of these tests, so each pytest worker starts it once. Each
test puts back what it changes.
"""

import subprocess
import uuid
from collections.abc import Iterator

import pytest

from tests.conftest import wait_until
from tests.integration.crowdsec_lab import (
    BANNED,
    BOUNCER_CONFIG,
    SOCKET,
    STARTUP_TIMEOUT,
    Lab,
    logs,
    start,
)
from tests.integration.haproxy_live import docker


@pytest.fixture(scope="session")
def lab(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Lab]:
    """CrowdSec on, every test source banned, and the ban enforced."""
    prefix = f"homeproxy-test-crowdsec-{uuid.uuid4().hex[:12]}"
    try:
        running = start(prefix, tmp_path_factory.mktemp("seed"))

        def lapi_ready() -> bool:
            docker("exec", running.crowdsec, "cscli", "lapi", "status")
            return True

        wait_until(
            lapi_ready,
            "the CrowdSec LAPI",
            timeout=STARTUP_TIMEOUT,
            interval=0.5,
            not_yet=(Exception,),
        )
        for address in BANNED:
            docker("exec", running.crowdsec, "cscli", "decisions", "add", "--ip", address)
        wait_until(
            lambda: running.exists("/run/haproxy/admin.sock"),
            "HAProxy",
            timeout=STARTUP_TIMEOUT,
            interval=0.5,
            not_yet=(Exception,),
        )
        # The bouncer stays down until an apply enables CrowdSec.
        assert not running.exists(BOUNCER_CONFIG)
        assert not running.exists(SOCKET)
        running.apply(enabled=True)
        running.wait_for_ban()
        yield running
    except BaseException:
        for container in (f"{prefix}-homeproxy", f"{prefix}-crowdsec"):
            print(f"--- {container}\n{logs(container)[-6000:]}")
        raise
    finally:
        for kind, name in [
            ("container", f"{prefix}-homeproxy"),
            ("container", f"{prefix}-crowdsec"),
            ("volume", f"{prefix}-data"),
            ("network", f"{prefix}-net"),
        ]:
            subprocess.run(["docker", kind, "rm", "--force", name], capture_output=True)
