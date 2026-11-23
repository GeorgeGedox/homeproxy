"""`atomic_write` replaces a file whole, flushes it to disk, and keeps modes private."""

import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from homeproxy.util.atomic import atomic_write, ensure_dir


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_creates_a_new_file_with_private_mode(tmp_path: Path) -> None:
    target = tmp_path / "new.yaml"
    atomic_write(target, b"data\n")
    assert target.read_bytes() == b"data\n"
    assert mode_of(target) == 0o600


def test_new_file_mode_ignores_a_loose_umask(tmp_path: Path) -> None:
    previous = os.umask(0)
    try:
        atomic_write(tmp_path / "new", b"data")
    finally:
        os.umask(previous)
    assert mode_of(tmp_path / "new") == 0o600


def test_existing_file_keeps_its_mode(tmp_path: Path) -> None:
    target = tmp_path / "file"
    target.write_bytes(b"old")
    target.chmod(0o640)
    atomic_write(target, b"new")
    assert target.read_bytes() == b"new"
    assert mode_of(target) == 0o640


def test_explicit_mode_tightens_an_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "secrets.yaml"
    target.write_bytes(b"old")
    target.chmod(0o644)
    atomic_write(target, b"new", mode=0o600)
    assert mode_of(target) == 0o600


def test_success_leaves_no_temp_file(tmp_path: Path) -> None:
    atomic_write(tmp_path / "file", b"data")
    assert [path.name for path in tmp_path.iterdir()] == ["file"]


def test_temp_file_is_hidden_in_the_same_directory_and_never_matches_a_yaml_glob(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HAProxy loads every file in a `crt` directory unless its name starts with a dot."""
    seen: list[tuple[str, int]] = []
    real_replace = os.replace

    def spy(source: str, target: Path) -> None:
        temp = Path(source)
        assert temp.name.startswith(".")
        seen.append((str(temp.parent), mode_of(temp)))
        assert list(tmp_path.glob("*.yaml")) == []
        real_replace(source, target)

    monkeypatch.setattr(os, "replace", spy)
    atomic_write(tmp_path / "route.yaml", b"data", mode=0o600)
    assert seen == [(str(tmp_path), 0o600)]


@pytest.mark.parametrize("failing", ["fsync", "replace"])
def test_failure_keeps_the_old_file_and_removes_the_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    target = tmp_path / "file"
    target.write_bytes(b"old")

    def fail(*_args: Any) -> None:
        raise OSError("disk failed")

    monkeypatch.setattr(os, failing, fail)
    with pytest.raises(OSError, match="disk failed"):
        atomic_write(target, b"new")
    assert target.read_bytes() == b"old"
    assert [path.name for path in tmp_path.iterdir()] == ["file"]


def test_data_is_flushed_before_the_rename_and_the_directory_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def spy(name: str, real: Callable[..., None]) -> Callable[..., None]:
        def wrapper(*args: Any) -> None:
            if name == "fsync":
                is_dir = stat.S_ISDIR(os.fstat(args[0]).st_mode)
                calls.append("fsync-dir" if is_dir else "fsync-file")
            else:
                calls.append(name)
            real(*args)

        return wrapper

    monkeypatch.setattr(os, "fsync", spy("fsync", os.fsync))
    monkeypatch.setattr(os, "replace", spy("replace", os.replace))
    atomic_write(tmp_path / "file", b"data")
    assert calls == ["fsync-file", "replace", "fsync-dir"]


def test_missing_parent_raises_and_creates_nothing(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        atomic_write(tmp_path / "missing" / "file", b"data")
    assert list(tmp_path.iterdir()) == []


def test_ensure_dir_creates_private_parents_and_accepts_an_existing_directory(
    tmp_path: Path,
) -> None:
    target = tmp_path / "one" / "two"
    ensure_dir(target)
    ensure_dir(target)
    assert mode_of(target) == 0o700
    assert mode_of(tmp_path / "one") == 0o700
