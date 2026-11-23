"""Install a bundle in `/data/certs/` and make HAProxy serve it.

HAProxy loads the whole directory with `crt /data/certs/`. A renewed certificate is already
loaded, so it is hot-swapped through the runtime API with no reload. A new certificate needs one
reload, which drops no connections. The file is always written first, so a later reload or
restart serves the same certificate as the hot swap.
"""

import logging
import re
from enum import StrEnum
from pathlib import Path

from homeproxy.certs.errors import CertificateError
from homeproxy.generator.certificate import ensure_default_certificate
from homeproxy.generator.render import CERTS_DIR
from homeproxy.haproxy.client import HAProxyClient, HAProxyCommandError, HAProxyError
from homeproxy.store.files import Store
from homeproxy.util.atomic import atomic_write

logger = logging.getLogger(__name__)

# HAProxy allows one certificate transaction at a time. One left open by a crash names its file.
_ONGOING = re.compile(r"The ongoing transaction is about '([^']+)'")


class Delivery(StrEnum):
    """How HAProxy got the bundle."""

    UNCHANGED = "unchanged"
    HOT_SWAPPED = "hot-swapped"
    RELOADED = "reloaded"


def install_bundle(
    name: str, bundle: bytes, *, certs_dir: Path, client: HAProxyClient, store: Store
) -> Delivery:
    """Write `<name>.pem` and make HAProxy serve it, unless the same bundle is already there.

    Holds the store lock, so this never overlaps an apply or another certificate's reload.
    Raises `CertificateError` if HAProxy could not be updated.
    """
    target = certs_dir / f"{name}.pem"
    with store.lock():
        previous = target.read_bytes() if target.exists() else None
        if previous == bundle:
            return Delivery.UNCHANGED
        atomic_write(target, bundle, mode=0o600)
        swapped = previous is not None and _hot_swap(
            client, f"{CERTS_DIR}/{target.name}", bundle.decode("ascii")
        )
        if swapped:
            return Delivery.HOT_SWAPPED
        _reload(client, certs_dir, target, previous)
        return Delivery.RELOADED


def _hot_swap(client: HAProxyClient, runtime_name: str, pem: str) -> bool:
    """Replace a loaded certificate. Return False if HAProxy refused, so the caller reloads.

    A transaction on another certificate, left open by a crash, blocks every hot swap until the
    next reload, so it is aborted and the swap is tried once more.
    """
    try:
        try:
            client.replace_ssl_cert(runtime_name, pem)
        except HAProxyCommandError as error:
            stale = _ONGOING.search(str(error))
            if stale is None:
                raise
            logger.warning("aborting a stale certificate transaction on %s", stale[1])
            client.abort_ssl_cert(stale[1])
            client.replace_ssl_cert(runtime_name, pem)
    except HAProxyError as error:
        # A file written but never loaded, for example after a failed reload, ends up here.
        logger.warning("hot swap of %s failed, reloading instead: %s", runtime_name, error)
        return False
    return True


def _reload(client: HAProxyClient, certs_dir: Path, target: Path, previous: bytes | None) -> None:
    """Reload HAProxy so it loads the directory again. Raises `CertificateError` on failure.

    A refused reload puts back the `previous` bundle, or removes a new file, because the old
    worker still serves and the next start must not load what HAProxy refused.
    """
    # The generated config names the default certificate, and a reload without it fails.
    ensure_default_certificate(certs_dir)
    try:
        result = client.reload()
    except HAProxyError as error:
        raise CertificateError(f"HAProxy did not confirm the reload: {error}") from error
    if result.success:
        return
    if previous is None:
        target.unlink(missing_ok=True)
    else:
        atomic_write(target, previous, mode=0o600)
    raise CertificateError(f"HAProxy refused the reload: {result.startup_log[-2000:]}")
