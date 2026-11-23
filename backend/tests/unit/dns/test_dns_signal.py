"""Signalling the DNS process never fails an apply."""

from pathlib import Path

import pytest

from homeproxy.dns.signal import reload_dns


@pytest.mark.parametrize("pid", [None, "999999999\n"], ids=["not-running", "stale-pid"])
def test_reload_without_a_running_dns_process_does_not_raise(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, pid: str | None
) -> None:
    pid_file = tmp_path / "dns.pid"
    if pid is not None:
        pid_file.write_text(pid)

    reload_dns(pid_file)

    assert ("stale DNS pid file" in caplog.text) == (pid is not None)
