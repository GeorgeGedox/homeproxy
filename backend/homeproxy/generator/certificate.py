"""The generated default certificate, so `bind ssl` works before the first issuance."""

import tempfile
from pathlib import Path

from homeproxy.util.atomic import atomic_write
from homeproxy.util.run import run

DEFAULT_CERTIFICATE_NAME = "default.pem"
"""A zone has at least two labels, so this name never equals an issued `<zone>.pem`."""

_VALID_DAYS = "3650"
_SUBJECT = "/CN=HomeProxy default certificate"


def ensure_default_certificate(certs_dir: Path) -> Path:
    """Create the default certificate in `certs_dir` if it is missing, and return its path.

    The generated config names this file, so the apply pipeline calls this function before it runs
    `haproxy -c`. An existing file is never changed. The file holds a self-signed certificate and
    its private key, with mode 0600. It stays outside the release directory, so a private key never
    appears in a release diff, and rendering stays byte-for-byte repeatable.

    Raises `homeproxy.util.run.RunError` if `openssl` fails.
    """
    target = certs_dir / DEFAULT_CERTIFICATE_NAME
    if target.exists():
        return target
    certs_dir.mkdir(parents=True, exist_ok=True)
    # HAProxy ignores names that start with a dot, so a crash here cannot break the next reload.
    with tempfile.TemporaryDirectory(dir=certs_dir, prefix=".default-") as work_dir:
        key = Path(work_dir) / "key.pem"
        certificate = Path(work_dir) / "certificate.pem"
        run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "ec",
                "-pkeyopt",
                "ec_paramgen_curve:P-256",
                "-nodes",
                "-keyout",
                str(key),
                "-out",
                str(certificate),
                "-days",
                _VALID_DAYS,
                "-subj",
                _SUBJECT,
            ]
        )
        atomic_write(target, certificate.read_bytes() + key.read_bytes(), mode=0o600)
    return target
