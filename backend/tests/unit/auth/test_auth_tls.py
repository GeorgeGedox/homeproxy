"""Tests for the self-signed dashboard certificate."""

import datetime
import hashlib
import ipaddress
import os
import ssl
import stat
from pathlib import Path

from cryptography import x509

from homeproxy.auth.tls import create_certificate, ensure_certificate, fingerprint


def _certificate(path: Path) -> x509.Certificate:
    return x509.load_pem_x509_certificates(path.read_bytes())[0]


def test_new_certificate_is_private_loadable_and_names_the_address(tmp_path: Path) -> None:
    """The file is 0600, loads as a TLS server pair, and names the listen address."""
    path = tmp_path / "tls" / "dashboard.pem"
    assert ensure_certificate(path, ["192.168.1.10", "0.0.0.0"])  # noqa: S104
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(path)
    names = _certificate(path).extensions.get_extension_for_class(x509.SubjectAlternativeName)
    addresses = names.value.get_values_for_type(x509.IPAddress)
    assert ipaddress.ip_address("192.168.1.10") in addresses
    assert ipaddress.ip_address("127.0.0.1") in addresses
    assert ipaddress.ip_address("0.0.0.0") not in addresses  # noqa: S104
    assert "homeproxy" in names.value.get_values_for_type(x509.DNSName)


def test_existing_certificate_is_kept(tmp_path: Path) -> None:
    """A second start reuses the file, so the browser exception keeps working."""
    path = tmp_path / "dashboard.pem"
    ensure_certificate(path, ["192.168.1.10"])
    before = path.read_bytes()
    assert not ensure_certificate(path, ["10.0.0.5"])
    assert path.read_bytes() == before


def test_broken_certificate_is_replaced(tmp_path: Path) -> None:
    """A truncated file is replaced by a working one."""
    path = tmp_path / "dashboard.pem"
    path.write_bytes(b"-----BEGIN CERTIFICATE-----\nAAAA\n")
    assert ensure_certificate(path, [])
    ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(path)


def test_expired_certificate_is_replaced(tmp_path: Path) -> None:
    """A certificate past its end date is made again."""
    path = tmp_path / "dashboard.pem"
    old = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=4000)
    path.write_bytes(create_certificate([], now=old))
    os.chmod(path, 0o600)
    assert ensure_certificate(path, [])
    assert _certificate(path).not_valid_after_utc > datetime.datetime.now(datetime.UTC)


def test_fingerprint_matches_the_certificate(tmp_path: Path) -> None:
    """The printed fingerprint is the SHA-256 of the certificate."""
    path = tmp_path / "dashboard.pem"
    ensure_certificate(path, [])
    printed = fingerprint(path)
    der = ssl.PEM_cert_to_DER_cert(
        path.read_text()[path.read_text().index("-----BEGIN CERTIFICATE-----") :]
    )
    assert printed.replace(":", "").lower() == hashlib.sha256(der).hexdigest()
