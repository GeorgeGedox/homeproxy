"""Which certificates the config needs: one wildcard per domain, and one per host outside it."""

from collections.abc import Iterable
from dataclasses import dataclass

from homeproxy.models import Config, Domain, HttpRoute, RedirectRoute, Route
from homeproxy.models.domain import domain_of


@dataclass(frozen=True)
class CertificateSpec:
    """One certificate. `name` is the zone or host: the lego name and the bundle's file stem.

    `provider` and `credentials` are those of the domain that owns it. A `self-signed` one has no
    credentials.
    """

    name: str
    domains: tuple[str, ...]
    zone: str
    provider: str
    credentials: str | None


def _covered(host: str, domain: Domain) -> bool:
    """Return whether the zone's wildcard certificate covers `host`: the apex or one label below."""
    if not domain.wildcard_cert:
        return False
    return host == domain.zone or "." not in host.removesuffix(f".{domain.zone}")


def _spec(name: str, domains: tuple[str, ...], domain: Domain) -> CertificateSpec | None:
    """Return the certificate for `domains`, or None for a domain with no certificate."""
    if domain.dns_provider is None:
        return None
    return CertificateSpec(name, domains, domain.zone, domain.dns_provider, domain.credentials)


def plan_certificates(config: Config, routes: Iterable[Route]) -> list[CertificateSpec]:
    """Return the certificates for every domain, `http` and `redirect` route, sorted by name.

    Passthrough and tcp routes keep their backend's certificate, so they need none. A domain with
    no DNS provider gets none, so lego never runs for it. A host outside every domain is skipped,
    because the apply pipeline rejects it.
    """
    specs = {
        d.zone: _spec(d.zone, (d.zone, f"*.{d.zone}"), d) for d in config.domains if d.wildcard_cert
    }
    for route in routes:
        if not isinstance(route, HttpRoute | RedirectRoute):
            continue
        for host in route.hosts:
            domain = domain_of(host, config.domains)
            if domain is None or _covered(host, domain):
                continue
            specs[host] = _spec(host, (host,), domain)
    return [spec for name in sorted(specs) if (spec := specs[name]) is not None]
