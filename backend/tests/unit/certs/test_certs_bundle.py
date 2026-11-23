import datetime
import logging
import re
from pathlib import Path

import pytest
from cryptography import x509

from homeproxy.certs.bundle import (
    ExpiryState,
    build_bundle,
    certificate_info,
    list_certificates,
)
from homeproxy.certs.errors import CertificateError
from homeproxy.certs.selfsigned import self_signed_bundle

DOMAINS = ("example.com", "*.example.com")
_KEY = re.compile(rb"-----BEGIN PRIVATE KEY-----.*?-----END PRIVATE KEY-----\n", re.DOTALL)


def split(bundle: bytes) -> tuple[bytes, bytes]:
    """Return the chain and the key of a bundle."""
    key = _KEY.search(bundle)
    assert key is not None
    return bundle.replace(key[0], b""), key[0]


def test_bundle_is_the_chain_then_the_key() -> None:
    chain, key = split(self_signed_bundle(DOMAINS))

    assert build_bundle(chain, key) == chain + key


@pytest.mark.parametrize(
    ("chain", "key", "message"),
    [
        pytest.param(b"", b"", "cannot be read", id="empty"),
        pytest.param(None, None, "does not match", id="key-of-another-certificate"),
    ],
)
def test_bundle_that_haproxy_would_refuse_is_an_error(
    chain: bytes | None, key: bytes | None, message: str
) -> None:
    first, _ = split(self_signed_bundle(DOMAINS))
    _, other_key = split(self_signed_bundle(DOMAINS))

    with pytest.raises(CertificateError, match=message) as failure:
        build_bundle(first if chain is None else chain, other_key if key is None else key)

    assert "PRIVATE KEY" not in str(failure.value)


@pytest.mark.parametrize(
    ("days_left", "state"),
    [
        (22, ExpiryState.OK),
        (21, ExpiryState.WARNING),
        (8, ExpiryState.WARNING),
        (7, ExpiryState.ALERT),
        (-1, ExpiryState.ALERT),
    ],
)
def test_info_warns_at_21_days_and_alerts_at_7(
    tmp_path: Path, days_left: int, state: ExpiryState
) -> None:
    path = tmp_path / "example.com.pem"
    path.write_bytes(self_signed_bundle(DOMAINS))
    expires = x509.load_pem_x509_certificate(path.read_bytes()).not_valid_after_utc
    now = expires - datetime.timedelta(days=days_left, hours=1)

    info = certificate_info(path, now)

    assert (info.name, info.names, info.not_after) == ("example.com", DOMAINS, expires)
    assert (info.days_left, info.state) == (days_left, state)
    assert info.issuer == "CN=example.com"


def test_list_shows_issued_certificates_only(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    for name in ("b.example.com.pem", "a.example.com.pem", "default.pem", ".hidden.pem"):
        (tmp_path / name).write_bytes(self_signed_bundle((name.removesuffix(".pem"),)))
    (tmp_path / "broken.example.com.pem").write_text("not a certificate\n")

    with caplog.at_level(logging.WARNING):
        listed = list_certificates(tmp_path)

    assert [info.name for info in listed] == ["a.example.com", "b.example.com"]
    assert "broken.example.com.pem" in caplog.text
