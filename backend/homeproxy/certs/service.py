"""Issue, renew and install the certificates the config needs, from a job's worker thread."""

import logging
import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from homeproxy.certs.bundle import CertificateInfo, build_bundle, list_certificates
from homeproxy.certs.deploy import Delivery, install_bundle
from homeproxy.certs.errors import CertificateError
from homeproxy.certs.lego import LETSENCRYPT, LETSENCRYPT_STAGING, Lego
from homeproxy.certs.plan import CertificateSpec, plan_certificates
from homeproxy.certs.providers import SELF_SIGNED
from homeproxy.certs.selfsigned import self_signed_bundle, self_signed_current
from homeproxy.certs.token import check_token
from homeproxy.generator.certificate import ensure_default_certificate
from homeproxy.haproxy.client import HAProxyClient
from homeproxy.models.secrets import DnsCredential
from homeproxy.store.files import Store

logger = logging.getLogger(__name__)

DEV_SELF_SIGNED_ENV = "HOMEPROXY_DEV_SELF_SIGNED"
"""Set only by the dev stack: `1` makes self-signed certificates offline, `0` uses Let's Encrypt
staging. Unset, which is production, `certificates.staging` in the applied config picks the CA."""


@dataclass
class CertificateService:
    """Certificates for one data directory and one HAProxy. `lego` is None in self-signed mode.

    `staging`, when set, replaces `lego` while the applied config has `certificates.staging` on.
    Each CA has its own lego directory, so a switch issues every certificate from the new CA.
    """

    store: Store
    client: HAProxyClient
    lego: Lego | None
    certs_dir: Path
    staging: Lego | None = None
    # One lego run at a time, so a manual renewal and the daily job never share lego's state.
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    @classmethod
    def from_environ(
        cls, data_root: Path, client: HAProxyClient, environ: Mapping[str, str] = os.environ
    ) -> "CertificateService":
        """Build the service for `data_root`. `HOMEPROXY_DEV_SELF_SIGNED` overrides the setting."""
        acme, secrets = data_root / "acme", data_root / "secrets"
        staging = Lego(acme / "staging", server=LETSENCRYPT_STAGING, secrets_dir=secrets)
        store, certs_dir = Store(data_root), data_root / "certs"
        mode = environ.get(DEV_SELF_SIGNED_ENV)
        if mode == "1":
            return cls(store, client, None, certs_dir)
        if mode is not None:
            return cls(store, client, staging, certs_dir)
        production = Lego(acme, server=LETSENCRYPT, secrets_dir=secrets)
        return cls(store, client, production, certs_dir, staging=staging)

    def plan(self) -> list[CertificateSpec]:
        """Return the certificates the current config needs."""
        return plan_certificates(self.store.load_config(), self.store.load_routes())

    def certificates(self) -> list[CertificateInfo]:
        """Return the installed certificates with their issuer, names and expiry."""
        return list_certificates(self.certs_dir)

    def needs_issue(self, spec: CertificateSpec) -> bool:
        """Whether the bundle is missing, or is not the certificate lego holds for the CA the
        applied config picks, as after a switch between production and staging. A self-signed
        bundle needs it when it is not current, as after a switch from lego."""
        installed = self.certs_dir / f"{spec.name}.pem"
        lego = self._lego(spec)
        if lego is None:
            return not self_signed_current(installed, spec.domains)
        if not installed.exists():
            return True
        chain, key = lego.files(spec.name)
        try:
            return installed.read_bytes() != build_bundle(chain.read_bytes(), key.read_bytes())
        except (OSError, CertificateError):
            return True

    def renew(self, name: str, *, force: bool = False) -> Delivery:
        """Issue one certificate of the plan, or renew it if it is due or `force` is set.

        Raises `CertificateError` with a message to show the user.
        """
        spec = next((s for s in self.plan() if s.name == name), None)
        if spec is None:
            raise CertificateError(f"the config needs no certificate named {name}")
        return self._renew(spec, force=force)

    def renew_all(self) -> None:
        """Issue missing certificates and renew those due, for the daily job.

        One failure does not stop the others. Raises `CertificateError` naming each failure.
        """
        ensure_default_certificate(self.certs_dir)
        if not self.store.configured():
            return
        failures = []
        for spec in self.plan():
            try:
                self._renew(spec, force=False)
            except CertificateError as error:
                logger.error("certificate %s failed: %s", spec.name, error)
                failures.append(f"{spec.name}: {error}")
        if failures:
            raise CertificateError("; ".join(failures))

    def _renew(self, spec: CertificateSpec, *, force: bool) -> Delivery:
        with self._lock:
            bundle = self._obtain(spec, force=force)
            delivery = install_bundle(
                spec.name, bundle, certs_dir=self.certs_dir, client=self.client, store=self.store
            )
        logger.info("certificate %s: %s", spec.name, delivery)
        return delivery

    def _obtain(self, spec: CertificateSpec, *, force: bool) -> bytes:
        """Return the bundle to install, from lego or, when self-signed, made here."""
        lego = self._lego(spec)
        if lego is None:
            installed = self.certs_dir / f"{spec.name}.pem"
            if not force and self_signed_current(installed, spec.domains):
                return installed.read_bytes()
            return self_signed_bundle(spec.domains)
        credential = self._credential(spec)
        try:
            lego.obtain(spec, credential, force=force)
        except CertificateError:
            if credential.api_token is not None:
                # lego reports a missing permission as a zone it cannot find. Name it instead.
                check_token(spec.zone, credential.api_token.get_secret_value())
            raise
        chain, key = lego.files(spec.name)
        try:
            files = chain.read_bytes(), key.read_bytes()
        except OSError:
            raise CertificateError(f"lego wrote no certificate files for {spec.name}") from None
        return build_bundle(*files)

    def _lego(self, spec: CertificateSpec) -> Lego | None:
        """Return the lego of the CA the applied config picks, or None to make the certificate
        here: for a self-signed domain, whatever the CA, and in self-signed mode."""
        if spec.provider == SELF_SIGNED:
            return None
        if self.staging is not None and self.store.load_config().certificates.staging:
            return self.staging
        return self.lego

    def _credential(self, spec: CertificateSpec) -> DnsCredential:
        credentials = self.store.load_secrets().dns_credentials
        # The domain model gives every lego provider credentials.
        credential = credentials.get(spec.credentials) if spec.credentials else None
        if credential is None:
            raise CertificateError(
                f"the credentials {spec.credentials} of {spec.zone} are not in secrets.yaml"
            )
        if credential.provider != spec.provider:
            raise CertificateError(
                f"the credentials {spec.credentials} are for {credential.provider}, "
                f"but {spec.zone} uses {spec.provider}"
            )
        return credential
