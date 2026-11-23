"""The default certificate exists before the first issuance and never changes after that."""

import stat
from pathlib import Path

import pytest

from homeproxy.generator import DEFAULT_CERTIFICATE_NAME, ensure_default_certificate
from homeproxy.generator import certificate as certificate_module
from homeproxy.util.run import CommandNotStarted, run


def test_creates_a_private_bundle_with_certificate_and_key(tmp_path: Path) -> None:
    path = ensure_default_certificate(tmp_path / "certs")
    assert path.name == DEFAULT_CERTIFICATE_NAME
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    content = path.read_text()
    assert "BEGIN CERTIFICATE" in content
    assert "PRIVATE KEY" in content
    subject = run(["openssl", "x509", "-noout", "-subject", "-in", str(path)]).stdout
    assert "HomeProxy default certificate" in subject


def test_existing_certificate_is_never_changed(tmp_path: Path) -> None:
    path = ensure_default_certificate(tmp_path)
    before = path.read_bytes()
    assert ensure_default_certificate(tmp_path) == path
    assert path.read_bytes() == before


def test_leaves_no_temp_files(tmp_path: Path) -> None:
    ensure_default_certificate(tmp_path)
    assert [path.name for path in tmp_path.iterdir()] == [DEFAULT_CERTIFICATE_NAME]


def test_failure_creates_no_certificate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(argv: list[str]) -> None:
        raise CommandNotStarted("openssl is missing", tuple(argv))

    monkeypatch.setattr(certificate_module, "run", fail)
    with pytest.raises(CommandNotStarted):
        ensure_default_certificate(tmp_path)
    assert list(tmp_path.iterdir()) == []
