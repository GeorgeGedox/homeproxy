"""Models for `secrets.yaml`.

Every value is a `SecretStr`, so `repr` and `model_dump(mode="json")` mask it. A secret can still
reach a `ValidationError`, for example when it is pasted into a key position. Pass validation errors
through `homeproxy.models.safe_errors` before they reach a log or a response.
"""

import re
from typing import Annotated, Self
from urllib.parse import urlsplit

from pydantic import AfterValidator, Field, SecretStr, StringConstraints, model_validator

from homeproxy.certs.providers import CLOUDFLARE, SELF_SIGNED, variables
from homeproxy.models.base import StrictModel
from homeproxy.models.domain import DnsProvider
from homeproxy.models.fields import (
    BasicAuthHash,
    BasicAuthUser,
    Name,
    RequiredSecretToken,
    SecretsVersion,
    SecretToken,
    SessionKey,
    SigningKey,
)


def _env_value(value: SecretStr) -> SecretStr:
    secret = value.get_secret_value()
    if not secret or not secret.isprintable():
        raise ValueError("must be one line of printable characters")
    # JSON dumps show a secret as stars. Sending them back must not overwrite the real value.
    if not secret.strip("*"):
        raise ValueError("must not be the masked placeholder")
    return value


EnvName = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{0,127}$")]
"""An environment variable name as lego documents them. Membership in the catalog is checked too."""

EnvValue = Annotated[SecretStr, AfterValidator(_env_value)]
"""A credential value. It only reaches lego's environment, never a generated file, so quotes and
spaces are allowed."""


def check_credentials(
    provider: str, api_token: SecretStr | None, env: dict[str, SecretStr]
) -> None:
    """Raise `ValueError` unless the credentials fit the provider.

    Cloudflare takes `api_token`. Every other provider takes `env`, with only the names lego
    documents for it. The message never names a variable, because a secret can be pasted there.
    """
    if provider == CLOUDFLARE and (api_token is None or env):
        raise ValueError("cloudflare credentials take api_token and no env")
    if provider != CLOUDFLARE and api_token is not None:
        raise ValueError(f"api_token is only for cloudflare; {provider} takes env")
    known = variables()[provider]
    if not set(env) <= known:
        raise ValueError(f"env may only hold the variables lego documents for {provider}")


SECRET_NAME = re.compile(
    r"KEY|SECRET|TOKEN|PASS|AUTH|PRIVATE|CREDENTIAL|SIGNATURE|OTP|SERVICE_ACCOUNT"
)
"""Words in lego's variable names that mark a secret. `SERVICE_ACCOUNT` is Google's key JSON."""
MIN_SECRET_LENGTH = 8


def is_secret(name: str, value: str) -> bool:
    """Return whether a provider variable holds a secret, by the rule in `secret_values`."""
    return (
        bool(SECRET_NAME.search(name)) and len(value) >= MIN_SECRET_LENGTH and not value.isdigit()
    )


def _lego_provider(code: str) -> str:
    if code == SELF_SIGNED:
        raise ValueError(f"{SELF_SIGNED} takes no credentials")
    return code


class DnsCredential(StrictModel):
    """Credentials for one lego DNS provider.

    Cloudflare takes `api_token`, which needs Zone:Zone:Read and Zone:DNS:Edit. Any other provider
    takes `env`: the variables lego documents for it, listed by `GET /domains/providers`.
    """

    provider: Annotated[DnsProvider, AfterValidator(_lego_provider)]
    api_token: RequiredSecretToken | None = None
    env: dict[EnvName, EnvValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _fits_provider(self) -> Self:
        check_credentials(self.provider, self.api_token, dict(self.env))
        return self

    def secret_values(self) -> list[SecretStr]:
        """Return the values to register for redaction: the token and the `env` secrets.

        `redact` replaces every occurrence of a value, so a polling interval of `2` would garble
        every log line. An `env` value is a secret when its name holds a word such as KEY, TOKEN
        or PASSWORD (`SECRET_NAME`), it has at least `MIN_SECRET_LENGTH` characters, which leaves
        out booleans, and it is not only digits. A region, a URL or a user name is not registered.
        """
        secrets = [] if self.api_token is None else [self.api_token]
        return secrets + [v for n, v in self.env.items() if is_secret(n, v.get_secret_value())]


class SecretsCrowdsec(StrictModel):
    """CrowdSec bouncer and machine credentials, and captcha keys."""

    bouncer_key: SecretToken = SecretStr("")
    machine_id: SecretToken = SecretStr("")
    machine_password: SecretToken = SecretStr("")
    captcha_site_key: SecretToken = SecretStr("")
    captcha_secret_key: SecretToken = SecretStr("")
    captcha_signing_key: SigningKey = SecretStr("")


class SecretsGeoip(StrictModel):
    """GeoIP download credentials."""

    maxmind_license_key: SecretToken = SecretStr("")


_URL = re.compile(r"https?://[!-~]{1,2040}")


def _url(value: SecretStr) -> SecretStr:
    url = value.get_secret_value()
    if not url:
        return value
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 - reading it checks the port
    except ValueError:
        raise ValueError("must be an http or https URL with a valid port") from None
    if _URL.fullmatch(url) is None or not parts.hostname:
        raise ValueError("must be an http or https URL without spaces")
    # urllib would fail on credentials in the URL with an error that quotes them.
    if parts.username is not None or parts.password is not None:
        raise ValueError("must not hold a user name or password")
    return value


NotificationUrl = Annotated[SecretStr, AfterValidator(_url)]
"""A webhook or ping URL, or empty when unset. The URL is its own credential, so it is a secret."""


class TargetUrl(StrictModel):
    """The URL of one notification target."""

    url: NotificationUrl = Field(min_length=1)


class SecretsNotifications(StrictModel):
    """Notification URLs: one per target name, and the heartbeat's ping URL."""

    targets: dict[Name, TargetUrl] = Field(default_factory=dict)
    heartbeat_url: NotificationUrl = SecretStr("")

    def target_url(self, name: str) -> str:
        """Return the URL of a target, or `""` when it has none."""
        found = self.targets.get(name)
        return "" if found is None else found.url.get_secret_value()

    def urls(self) -> list[SecretStr]:
        """Return every URL, for redaction."""
        return [*(target.url for target in self.targets.values()), self.heartbeat_url]


def _backup_token_hash(value: SecretStr) -> SecretStr:
    if re.fullmatch(r"[0-9a-f]{64}", value.get_secret_value()) is None:
        raise ValueError("must be 64 lowercase hex digits")
    return value


BackupTokenHash = Annotated[SecretStr, AfterValidator(_backup_token_hash)]
"""The SHA-256 of the backup token, as 64 hex digits."""

BackupTokenTime = Annotated[
    str, StringConstraints(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
]
"""When the backup token was made, in UTC, as `2026-11-25T10:00:00Z`."""


class Secrets(StrictModel):
    """The whole of `secrets.yaml`."""

    version: SecretsVersion
    session_key: SessionKey
    dns_credentials: dict[Name, DnsCredential] = Field(default_factory=dict)
    basic_auth: dict[Name, dict[BasicAuthUser, BasicAuthHash]] = Field(default_factory=dict)
    crowdsec: SecretsCrowdsec = Field(default_factory=SecretsCrowdsec)
    geoip: SecretsGeoip = Field(default_factory=SecretsGeoip)
    notifications: SecretsNotifications = Field(default_factory=SecretsNotifications)
    backup_token_hash: BackupTokenHash | None = None
    backup_token_created_at: BackupTokenTime | None = None

    @model_validator(mode="after")
    def _token_has_time(self) -> Self:
        if (self.backup_token_hash is None) != (self.backup_token_created_at is None):
            raise ValueError("backup_token_hash and backup_token_created_at go together")
        return self
