"""Self-signed certificates, for domains with `dns_provider: self-signed` and for the offline dev
stack (`HOMEPROXY_DEV_SELF_SIGNED=1`)."""

import datetime
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from homeproxy.certs.lego import RENEW_DAYS

LIFETIME = datetime.timedelta(days=825)
"""The longest validity Apple platforms accept for a TLS server certificate. Each new certificate
needs the browser exception again, so it is replaced as seldom as possible."""


def self_signed_bundle(domains: tuple[str, ...]) -> bytes:
    """Return a new P-256 key and a self-signed certificate for `domains`, as a HAProxy bundle."""
    now = datetime.datetime.now(datetime.UTC)
    key = ec.generate_private_key(ec.SECP256R1())
    # A CN holds at most 64 characters. Clients match the SAN, so a longer name goes without one.
    names = domains[:1] if len(domains[0]) <= 64 else ()
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name) for name in names])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + LIFETIME)
        # RFC 5280 makes the SAN critical when the subject is empty.
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(d) for d in domains]), not subject)
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return certificate.public_bytes(serialization.Encoding.PEM) + key_pem


def self_signed_current(path: Path, domains: tuple[str, ...]) -> bool:
    """Return whether `path` holds a self-signed certificate for exactly `domains` with more than
    `RENEW_DAYS` left, the rule lego renews by. A missing or unreadable file is not current."""
    try:
        leaf = x509.load_pem_x509_certificates(path.read_bytes())[0]
        names = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except (OSError, ValueError, x509.ExtensionNotFound):
        return False
    left = leaf.not_valid_after_utc - datetime.datetime.now(datetime.UTC)
    return (
        leaf.issuer == leaf.subject
        and set(names.get_values_for_type(x509.DNSName)) == set(domains)
        and left.days > RENEW_DAYS
    )
