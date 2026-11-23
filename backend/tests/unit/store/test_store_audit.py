"""Audit entries survive a restart, stay on one line, hold no secrets and rotate by size."""

import logging
import os
import secrets
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from homeproxy.store import AuditLog, Store
from homeproxy.util.redact import REDACTED, register_secret


def test_entry_is_read_back_with_its_fields(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit")
    log.append("admin", "apply", {"snapshot": "20261109T120000Z"})
    [entry] = log.read()
    assert (entry.user, entry.action) == ("admin", "apply")
    assert entry.details == {"snapshot": "20261109T120000Z"}
    assert entry.time.endswith("+00:00")


def test_entries_are_newest_first_and_limited(tmp_path: Path) -> None:
    log = AuditLog(tmp_path)
    for index in range(5):
        log.append("admin", f"action-{index}")
    assert [entry.action for entry in log.read(limit=2)] == ["action-4", "action-3"]


def test_reading_nothing(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "missing")
    assert log.read() == []
    assert not (tmp_path / "missing").exists()
    log.append("admin", "apply")
    assert log.read(limit=0) == []


def test_entries_survive_a_process_killed_right_after_the_append(tmp_path: Path) -> None:
    code = """
        import os
        import signal
        import sys
        from pathlib import Path
        from homeproxy.store import Store
        Store(Path(sys.argv[1])).audit.append("admin", "apply", {"snapshot": "one"})
        os.kill(os.getpid(), signal.SIGKILL)
    """
    completed = subprocess.run(  # noqa: S603 - fixed arguments in a test
        [sys.executable, "-c", textwrap.dedent(code), str(tmp_path)], check=False
    )
    assert completed.returncode == -9

    store = Store(tmp_path)
    store.audit.append("admin", "unban")
    assert [entry.action for entry in store.audit.read()] == ["unban", "apply"]
    assert store.audit.read()[1].details == {"snapshot": "one"}


def test_append_flushes_the_file_and_a_new_directory_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flushed: list[str] = []
    real_fsync = os.fsync

    def spy(descriptor: int) -> None:
        is_dir = stat.S_ISDIR(os.fstat(descriptor).st_mode)
        flushed.append("dir" if is_dir else "file")
        real_fsync(descriptor)

    log = AuditLog(tmp_path)
    monkeypatch.setattr(os, "fsync", spy)
    log.append("admin", "first")
    assert "file" in flushed
    assert flushed[-1] == "dir"

    flushed.clear()
    log.append("admin", "second")
    assert flushed == ["file"]


def test_file_is_private_and_appended_to_not_rewritten(tmp_path: Path) -> None:
    log = AuditLog(tmp_path)
    log.append("admin", "first")
    path = tmp_path / "audit.jsonl"
    inode = path.stat().st_ino
    first = path.read_bytes()
    log.append("admin", "second")
    assert path.stat().st_ino == inode
    assert path.read_bytes().startswith(first)
    assert path.stat().st_mode & 0o777 == 0o600


def test_newline_in_a_value_cannot_forge_an_entry(tmp_path: Path) -> None:
    log = AuditLog(tmp_path)
    forged = '{"time":"t","user":"root","action":"forged"}'
    log.append(f"admin\n{forged}", f"apply\n{forged}", {"note": f"\n{forged}\n"})
    assert len((tmp_path / "audit.jsonl").read_bytes().splitlines()) == 1
    [entry] = log.read()
    assert entry.user == f"admin\n{forged}"


def test_registered_secret_never_reaches_the_file(tmp_path: Path) -> None:
    secret = "topsecret" + secrets.token_hex(16)
    register_secret(secret)
    log = AuditLog(tmp_path)
    log.append(secret, f"action {secret}", {"token": secret, secret: [secret]})
    raw = (tmp_path / "audit.jsonl").read_text()
    assert secret not in raw
    assert REDACTED in raw


def test_secret_registered_after_the_write_is_redacted_on_read(tmp_path: Path) -> None:
    secret = "topsecret" + secrets.token_hex(16)
    log = AuditLog(tmp_path)
    log.append("admin", "apply", {"note": secret})
    register_secret(secret)
    assert log.read()[0].details == {"note": REDACTED}


def test_unserializable_details_record_nothing(tmp_path: Path) -> None:
    log = AuditLog(tmp_path)
    with pytest.raises(TypeError):
        log.append("admin", "apply", {"value": object()})
    assert log.read() == []


def test_torn_last_line_does_not_swallow_the_next_entry(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    log = AuditLog(tmp_path)
    log.append("admin", "before")
    torn_secret = "fragment" + secrets.token_hex(8)
    with (tmp_path / "audit.jsonl").open("ab") as handle:
        handle.write(f'{{"time":"t","user":"{torn_secret}'.encode())
    log.append("admin", "after")

    with caplog.at_level(logging.WARNING):
        assert [entry.action for entry in log.read()] == ["after", "before"]
    assert "skipped 1 malformed" in caplog.text
    assert torn_secret not in caplog.text


@pytest.mark.parametrize(
    "line",
    [
        b"not json",
        b"[1]",
        b'{"time":"t","user":"u"}',
        b'{"time":1,"user":"u","action":"a"}',
        b'{"time":"t","user":"u","action":"a","details":[]}',
        b"\xff\xfe",
        b"[" * 100_000,
    ],
)
def test_malformed_line_is_skipped(tmp_path: Path, line: bytes) -> None:
    log = AuditLog(tmp_path)
    log.append("admin", "good")
    with (tmp_path / "audit.jsonl").open("ab") as handle:
        handle.write(line + b"\n")
    assert [entry.action for entry in log.read()] == ["good"]


def test_rotates_by_size_and_reads_across_files(tmp_path: Path) -> None:
    log = AuditLog(tmp_path, max_bytes=300, rotated_files=10)
    for index in range(20):
        log.append("admin", f"action-{index:02}")
    assert (tmp_path / "audit.jsonl.1").exists()
    assert (tmp_path / "audit.jsonl").stat().st_size <= 300
    assert [entry.action for entry in log.read(limit=20)] == [
        f"action-{index:02}" for index in range(19, -1, -1)
    ]


def test_rotation_drops_only_the_oldest_file(tmp_path: Path) -> None:
    log = AuditLog(tmp_path, max_bytes=100, rotated_files=2)
    for index in range(10):
        log.append("admin", f"action-{index}")
    assert sorted(path.name for path in tmp_path.glob("audit.jsonl*")) == [
        "audit.jsonl",
        "audit.jsonl.1",
        "audit.jsonl.2",
    ]
    actions = [entry.action for entry in log.read(limit=100)]
    assert actions == [f"action-{index}" for index in range(9, 6, -1)]


def test_entry_bigger_than_the_limit_is_still_recorded(tmp_path: Path) -> None:
    log = AuditLog(tmp_path, max_bytes=10)
    log.append("admin", "big")
    log.append("admin", "bigger")
    assert [entry.action for entry in log.read()] == ["bigger", "big"]


def test_empty_file_is_not_rotated_over_an_older_one(tmp_path: Path) -> None:
    log = AuditLog(tmp_path, max_bytes=10)
    (tmp_path / "audit.jsonl").touch()
    log.append("admin", "big")
    assert not (tmp_path / "audit.jsonl.1").exists()


def test_rotated_files_must_be_positive(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="rotated_files"):
        AuditLog(tmp_path, rotated_files=0)
