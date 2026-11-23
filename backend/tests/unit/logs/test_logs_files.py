"""The `access` and `haproxy` files that the API writes from the collector's lines."""

import logging
from pathlib import Path

import pytest

from homeproxy.logs import LogCollector
from homeproxy.logs.files import ROTATED_FILES, LogFiles
from homeproxy.util.redact import REDACTED, register_secret

LINE = (
    b"<134>1 2026-11-09T17:08:10.587251+00:00 - haproxy 8 - - "
    b"172.17.0.1:60834 [09/Nov/2026:17:08:10.586] fe_https~ route-app/192.168.1.20:8080 "
    b'0/0/1/2/3 200 512 - - ---- 2/1/0/0/0 0/0 "GET /?key=registered-secret HTTP/1.1" "app" "" ""\n'
)
NOTICE = b"<133>1 2026-11-09T17:08:10.587251+00:00 - haproxy 8 - - Proxy fe_https started.\n"


def collect(root: Path, files: LogFiles) -> LogCollector:
    """A collector that passes its lines to `files`, as the API wires them."""
    collector = LogCollector(root / "syslog.sock", other=files.haproxy)
    collector.subscribe(files.access)
    return collector


def test_access_lines_and_haproxy_messages_go_to_their_own_files_redacted(tmp_path: Path) -> None:
    files = LogFiles(tmp_path)
    collector = collect(tmp_path, files)

    register_secret("registered-secret")
    collector.receive(LINE)
    collector.receive(NOTICE)
    files.close()

    logs = tmp_path / "state" / "logs"
    message = LINE.decode().split(" - - ", 1)[1].replace("registered-secret", REDACTED)
    assert (logs / "access" / "access.log").read_text() == (
        f"2026-11-09T17:08:10.587251+00:00 {message}"
    )
    assert (logs / "haproxy" / "haproxy.log").read_text() == (
        "2026-11-09T17:08:10.587251+00:00 Proxy fe_https started.\n"
    )


def test_rotation_keeps_the_access_file_within_its_cap(tmp_path: Path) -> None:
    files = LogFiles(tmp_path, access_max_bytes=4096)
    collector = collect(tmp_path, files)

    # About 50 KB, more than the cap of the current file and every rotated one.
    for _ in range(200):
        collector.receive(LINE)
    files.close()

    directory = tmp_path / "state" / "logs" / "access"
    kept = sorted(directory.iterdir())
    assert [path.name for path in kept] == [
        "access.log",
        *(f"access.log.{index}" for index in range(1, ROTATED_FILES + 1)),
    ]
    assert max(path.stat().st_size for path in kept) <= 4096


def test_a_file_that_cannot_be_written_is_reported_once_and_again_when_it_recovers(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    logs = tmp_path / "state" / "logs"
    logs.mkdir(parents=True)
    # A file where the directory belongs fails every write, as a full disk does.
    (logs / "access").write_text("")
    files = LogFiles(tmp_path)
    collector = collect(tmp_path, files)

    with caplog.at_level(logging.INFO, logger="homeproxy.logs.files"):
        for _ in range(3):
            collector.receive(LINE)
        (logs / "access").unlink()
        for _ in range(2):
            collector.receive(LINE)
    files.close()

    assert [record.levelname for record in caplog.records] == ["ERROR", "INFO"]
    assert len(collector.tail()) == 5
    assert (logs / "access" / "access.log").read_text().count("\n") == 2
