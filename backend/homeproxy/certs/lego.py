"""The lego wrapper: DNS-01 through the domain's provider, checked against public resolvers.

lego runs with a fixed argument list and a minimal environment: a fixed `PATH`, a private `HOME`,
the `settings` of the code that builds `Lego`, and the credential's variables. Nothing else from
the API process reaches it. Credentials never appear on a command line.
`lego run` issues a certificate that does not exist yet, renews one with 30 days or less left, and
otherwise changes nothing.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from homeproxy.certs.errors import CertificateError
from homeproxy.certs.plan import CertificateSpec
from homeproxy.certs.providers import check_paths
from homeproxy.models.secrets import DnsCredential, is_secret
from homeproxy.util.run import CommandFailed, CommandTimedOut, RunError, run

LETSENCRYPT = "https://acme-v02.api.letsencrypt.org/directory"
LETSENCRYPT_STAGING = "https://acme-staging-v02.api.letsencrypt.org/directory"

PUBLIC_RESOLVERS = ("1.1.1.1:53",)
"""lego checks that the TXT record has propagated. The internal DNS server or AdGuard may never
show it, so lego always asks a public resolver."""

SYSTEM_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
RENEW_DAYS = 30
LEGO_TIMEOUT = 900.0
"""Propagation can take minutes. lego gives up on its own after a few, so this only stops a hang."""

_ERROR_LINE = re.compile(r'level=ERROR msg=\S+ error="(.*)"$', re.MULTILINE)
_MESSAGE_LIMIT = 1000


@dataclass(frozen=True)
class Lego:
    """lego with its state directory, ACME server and resolvers.

    `secrets_dir` is the only directory a credential's path variable may name. `settings` are
    variables for every run, chosen by code and never by an admin, such as a test CA.
    """

    acme_dir: Path
    server: str = LETSENCRYPT
    resolvers: tuple[str, ...] = PUBLIC_RESOLVERS
    binary: str = "lego"
    secrets_dir: Path = Path("/data/secrets")
    settings: Mapping[str, str] = field(default_factory=dict)

    def files(self, name: str) -> tuple[Path, Path]:
        """Return the certificate chain and key files lego keeps for a certificate name."""
        directory = self.acme_dir / "certificates"
        return directory / f"{name}.crt", directory / f"{name}.key"

    def obtain(
        self, spec: CertificateSpec, credential: DnsCredential, *, force: bool = False
    ) -> None:
        """Issue the certificate through the credential's provider, or renew it if it is due or
        `force` is set.

        Raises `CertificateError` with lego's error message when lego fails.
        """
        plain, secret = _credential_env(credential, self.secrets_dir)
        # Some provider clients read config files under HOME, so it is a directory of our own.
        home = self.acme_dir / "home"
        home.mkdir(mode=0o700, parents=True, exist_ok=True)
        env = {"PATH": SYSTEM_PATH, "HOME": str(home), **self.settings, **plain}
        argv = [
            self.binary,
            "--log.format",
            "text",
            "run",
            "--accept-tos",
            "--path",
            str(self.acme_dir),
            "--server",
            self.server,
            "--dns",
            credential.provider,
            *(arg for resolver in self.resolvers for arg in ("--dns.resolvers", resolver)),
            *(arg for domain in spec.domains for arg in ("--domains", domain)),
            "--cert.name",
            spec.name,
            "--renew-days",
            str(RENEW_DAYS),
            # The daily job adds its own jitter.
            "--no-random-sleep",
        ]
        if force:
            argv.append("--renew-force")
        try:
            run(argv, timeout=LEGO_TIMEOUT, env=env, secret_env=secret, inherit_env=False)
        except RunError as error:
            raise CertificateError(f"lego failed for {spec.name}: {_reason(error)}") from error


def _credential_env(
    credential: DnsCredential, secrets_dir: Path
) -> tuple[dict[str, str], dict[str, str]]:
    """Return the credential's variables for lego as plain values and secrets to redact.

    Raises `CertificateError` if a path variable names a file outside `secrets_dir`.
    """
    if credential.api_token is not None:
        # lego needs Zone:Read to find the zone and DNS:Edit for the TXT record. One token has both.
        token = credential.api_token.get_secret_value()
        return {}, {"CLOUDFLARE_DNS_API_TOKEN": token, "CLOUDFLARE_ZONE_API_TOKEN": token}
    values = {name: value.get_secret_value() for name, value in credential.env.items()}
    try:
        check_paths(values, secrets_dir)
    except ValueError as error:
        raise CertificateError(str(error)) from None
    secret = {name: value for name, value in values.items() if is_secret(name, value)}
    return {name: value for name, value in values.items() if name not in secret}, secret


def _reason(error: RunError) -> str:
    """Return lego's own error message, or the end of its output. `run` has redacted it.

    lego 5 logs to stdout, so both streams are read.
    """
    if isinstance(error, CommandTimedOut):
        return f"no result after {error.timeout:.0f} s"
    if not isinstance(error, CommandFailed):
        return str(error)
    output = error.stdout + error.stderr
    errors = _ERROR_LINE.findall(output)
    if errors:
        return str(errors[-1]).replace('\\"', '"')[:_MESSAGE_LIMIT]
    lines = [line for line in output.splitlines() if line.strip()]
    return lines[-1][:_MESSAGE_LIMIT] if lines else f"exit status {error.returncode}"
