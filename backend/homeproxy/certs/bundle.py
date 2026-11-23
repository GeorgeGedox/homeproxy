"""HAProxy PEM bundles (chain, then key) and the expiry data the UI shows for each one."""

import datetime
import logging
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from homeproxy.certs.errors import CertificateError
from homeproxy.generator.certificate import DEFAULT_CERTIFICATE_NAME

logger = logging.getLogger(__name__)

WARN_DAYS = 21
ALERT_DAYS = 7


def build_bundle(chain: bytes, key: bytes) -> bytes:
    """Return the chain and key as one PEM bundle, after checking that the key fits the chain.

    HAProxy refuses a bundle whose key does not match, and a reload with it would fail.
    Raises `CertificateError`, whose text holds nothing from the files.
    """
    try:
        leaf = x509.load_pem_x509_certificates(chain)[0]
        private_key = load_pem_private_key(key, password=None)
    except (ValueError, TypeError, IndexError):
        raise CertificateError("lego wrote a certificate or key that cannot be read") from None
    if leaf.public_key() != private_key.public_key():
        raise CertificateError("lego wrote a key that does not match its certificate")
    return chain.rstrip(b"\n") + b"\n" + key.rstrip(b"\n") + b"\n"


class ExpiryState(StrEnum):
    """How close a certificate is to expiry: warn at 21 days left, alert at 7."""

    OK = "ok"
    WARNING = "warning"
    ALERT = "alert"


@dataclass(frozen=True)
class CertificateInfo:
    """What the UI shows for one installed certificate."""

    name: str
    issuer: str
    names: tuple[str, ...]
    not_after: datetime.datetime
    days_left: int
    state: ExpiryState


def certificate_info(path: Path, now: datetime.datetime | None = None) -> CertificateInfo:
    """Read the first certificate of a bundle. Raises `ValueError` or `OSError` if it cannot."""
    leaf = x509.load_pem_x509_certificates(path.read_bytes())[0]
    try:
        names = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        dns_names = tuple(names.value.get_values_for_type(x509.DNSName))
    except x509.ExtensionNotFound:
        dns_names = ()
    left = leaf.not_valid_after_utc - (now or datetime.datetime.now(datetime.UTC))
    days = left.days
    state = ExpiryState.OK
    if days <= ALERT_DAYS:
        state = ExpiryState.ALERT
    elif days <= WARN_DAYS:
        state = ExpiryState.WARNING
    return CertificateInfo(
        name=path.stem,
        issuer=leaf.issuer.rfc4514_string(),
        names=dns_names,
        not_after=leaf.not_valid_after_utc,
        days_left=days,
        state=state,
    )


def list_certificates(certs_dir: Path) -> list[CertificateInfo]:
    """Return every issued bundle in `certs_dir`, sorted by name.

    The generated default certificate and hidden files, such as a temp file, are left out. A file
    that cannot be read is logged and left out, so one bad file does not hide the others.
    """
    found = []
    for path in sorted(certs_dir.glob("*.pem")):
        if path.name.startswith(".") or path.name == DEFAULT_CERTIFICATE_NAME:
            continue
        try:
            found.append(certificate_info(path))
        except (OSError, ValueError, IndexError):
            logger.warning("cannot read the certificate %s", path.name)
    return found
