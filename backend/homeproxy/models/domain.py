"""The model for one zone in `config.yaml` under `domains`."""

from typing import Annotated, Self

from pydantic import AfterValidator, Field, ValidationError, model_validator
from pydantic_core import InitErrorDetails, PydanticCustomError

from homeproxy.certs.providers import CLOUDFLARE, SELF_SIGNED, variables
from homeproxy.models.base import StrictModel
from homeproxy.models.fields import Hostname, Name


def _provider(code: str) -> str:
    if code != SELF_SIGNED and code not in variables():
        raise ValueError(
            f"must be {SELF_SIGNED} or a DNS provider code of the bundled lego, "
            "other than exec or manual"
        )
    return code


DnsProvider = Annotated[str, AfterValidator(_provider)]
"""A lego DNS provider code, such as `cloudflare` or `route53`, or `self-signed`. `exec` and
`manual` are refused."""


class DomainCloudflare(StrictModel):
    """Cloudflare settings for one zone. They have no effect while the integration is off."""

    ddns_record: Hostname | None = None
    ddns_update: bool = True
    default_proxied: bool = True


class Domain(StrictModel):
    """One DNS zone: its certificate, DNS provider and credentials reference.

    A domain with neither `dns_provider` nor `credentials` has no certificate, and its routes are
    served over plain HTTP only. `dns_provider: self-signed` takes no credentials: HomeProxy makes
    its certificate.
    """

    zone: Hostname
    wildcard_cert: bool = True
    dns_provider: DnsProvider | None = None
    credentials: Name | None = None
    cloudflare: DomainCloudflare = Field(default_factory=DomainCloudflare)

    @model_validator(mode="before")
    @classmethod
    def _no_wildcard_without_provider(cls, data: object) -> object:
        """Default `wildcard_cert` to false when no provider is given: no certificate at all."""
        if isinstance(data, dict) and data.get("dns_provider") is None:
            return {"wildcard_cert": False, **data}
        return data

    @model_validator(mode="after")
    def _certificate_fields_agree(self) -> Self:
        if self.dns_provider == SELF_SIGNED and self.credentials is not None:
            _refuse("credentials", "a self-signed certificate takes no credentials")
        if self.dns_provider not in (None, SELF_SIGNED) and self.credentials is None:
            _refuse("credentials", "a DNS provider needs credentials")
        if self.dns_provider is None and self.credentials is not None:
            _refuse("dns_provider", "credentials need a DNS provider")
        if self.dns_provider is None and self.wildcard_cert:
            _refuse("wildcard_cert", "a wildcard certificate needs a DNS provider")
        return self

    @property
    def has_certificate(self) -> bool:
        """Return whether the domain gets certificates. Without one its routes are plain HTTP."""
        return self.dns_provider is not None

    @property
    def ddns_record(self) -> str:
        """Return the CNAME target for this zone's public routes, by default `home.<zone>`."""
        return self.cloudflare.ddns_record or f"home.{self.zone}"


def _refuse(field: str, message: str) -> None:
    """Raise a `ValidationError` that names `field`, not the whole domain."""
    error = PydanticCustomError("certificate_fields", message)
    details = InitErrorDetails(type=error, loc=(field,), input=None)
    raise ValidationError.from_exception_data("Domain", [details])


def cloudflare_off_reason(domain: Domain) -> str | None:
    """Return why Cloudflare DNS records, DDNS and the route switch are off for a domain, or None
    when its DNS is on Cloudflare."""
    if domain.dns_provider == CLOUDFLARE:
        return None
    if domain.dns_provider in (None, SELF_SIGNED):
        return f"{domain.zone} has no DNS provider"
    return f"{domain.zone} uses the DNS provider {domain.dns_provider}, not Cloudflare"


def in_zone(name: str, zone: str) -> bool:
    """Return whether `name` is `zone` or a name below it."""
    return name == zone or name.endswith(f".{zone}")


def domain_of(host: str, domains: list[Domain]) -> Domain | None:
    """Return the domain with the longest zone that contains `host`, so nested zones win."""
    owners = [d for d in domains if in_zone(host, d.zone)]
    return max(owners, key=lambda d: len(d.zone), default=None)
