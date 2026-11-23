"""The dashboard's self-signed TLS certificate at `/data/tls/dashboard.pem`.

Browsers drop `Secure` cookies over plain HTTP, and no public certificate exists at first run, so
the dashboard always serves HTTPS with a certificate it makes itself. The file holds the private
key and then the certificate, is mode 0600, and is reused on every start. It is made again only
when it is missing, unreadable or expired.
"""

import datetime
import ipaddress
import logging
import secrets
import ssl
from collections.abc import Iterable
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from homeproxy.util.atomic import atomic_write, ensure_dir

logger = logging.getLogger(__name__)

LIFETIME = datetime.timedelta(days=3650)
"""Self-signed certificates are not bound by the public 398-day limit. A long lifetime means the
browser exception the user accepted keeps working."""

COMMON_NAME = "HomeProxy dashboard"


def _expires(data: bytes) -> datetime.datetime | None:
    """Return the expiry of the first certificate in a PEM file, or None if there is none."""
    try:
        certificates = x509.load_pem_x509_certificates(data)
    except ValueError:
        return None
    return certificates[0].not_valid_after_utc if certificates else None


def _usable(path: Path, now: datetime.datetime) -> bool:
    """Return whether the file loads as a key and certificate pair that has not expired."""
    try:
        data = path.read_bytes()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(path)
    except (OSError, ssl.SSLError, ValueError):
        return False
    expires = _expires(data)
    return expires is not None and expires > now + datetime.timedelta(days=1)


def _subject_alt_names(addresses: Iterable[str]) -> x509.SubjectAlternativeName:
    names: list[x509.GeneralName] = [x509.DNSName("homeproxy"), x509.DNSName("localhost")]
    seen = {ipaddress.ip_address("127.0.0.1")}
    names.append(x509.IPAddress(ipaddress.ip_address("127.0.0.1")))
    for text in addresses:
        try:
            address = ipaddress.ip_address(text)
        except ValueError:
            continue
        if address.is_unspecified or address in seen:
            continue
        seen.add(address)
        names.append(x509.IPAddress(address))
    return x509.SubjectAlternativeName(names)


def create_certificate(addresses: Iterable[str], now: datetime.datetime | None = None) -> bytes:
    """Return a new P-256 key and a self-signed server certificate, as one PEM document.

    The certificate names `homeproxy`, `localhost`, 127.0.0.1 and each address given.
    """
    issued = now or datetime.datetime.now(datetime.UTC)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, COMMON_NAME)])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(int.from_bytes(secrets.token_bytes(16)) >> 1)
        .not_valid_before(issued - datetime.timedelta(days=1))
        .not_valid_after(issued + LIFETIME)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(_subject_alt_names(addresses), critical=False)
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return key_pem + certificate.public_bytes(serialization.Encoding.PEM)


def ensure_certificate(path: Path, addresses: Iterable[str]) -> bool:
    """Make sure `path` holds a usable key and certificate. Return True if a new one was made.

    An existing usable file is never replaced, so a certificate the user put there stays. The
    addresses only matter for a new certificate.
    """
    now = datetime.datetime.now(datetime.UTC)
    if _usable(path, now):
        return False
    if path.exists():
        logger.warning("dashboard certificate %s is unusable or expired; making a new one", path)
    ensure_dir(path.parent)
    atomic_write(path, create_certificate(addresses, now), mode=0o600)
    return True


def fingerprint(path: Path) -> str:
    """Return the SHA-256 fingerprint of the certificate in `path`, as colon-separated hex.

    Users compare it with what the browser shows before they accept the certificate.
    """
    certificates = x509.load_pem_x509_certificates(path.read_bytes())
    digest = certificates[0].fingerprint(hashes.SHA256())
    return ":".join(f"{byte:02X}" for byte in digest)
