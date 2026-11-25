"""Domain endpoints: CRUD in the pending change set, with a check of the credentials on save.

A domain edit is staged in `config.yaml` and reaches HAProxy at the next apply, which also starts
the issuance of its certificate. New credentials are written straight to `secrets.yaml` under the
domain's `credentials` name, because secrets have no pending copy. They replace the credentials of
every domain that uses that name, live or pending, so all of them must use the same provider, and
a Cloudflare token is checked against each of their zones first. Other providers are checked by
the first issuance. Credentials are never returned.
"""

from collections.abc import Mapping
from typing import Any, Self

from fastapi import APIRouter, Response
from pydantic import SecretStr, model_validator
from ruamel.yaml.comments import CommentedMap

from homeproxy.api.apply import ServicesDep
from homeproxy.api.changes import edit_lock
from homeproxy.api.errors import ApiError, not_found
from homeproxy.auth.routes import Runtime
from homeproxy.certs.errors import TokenError
from homeproxy.certs.providers import CLOUDFLARE, SELF_SIGNED, LegoProvider, catalog, check_paths
from homeproxy.certs.token import check_token
from homeproxy.models import Domain, TcpRoute
from homeproxy.models.domain import cloudflare_off_reason, domain_of, in_zone
from homeproxy.models.fields import RequiredSecretToken
from homeproxy.models.secrets import EnvName, EnvValue, check_credentials
from homeproxy.store import HeldLock, Store, load_tree, validate_config
from homeproxy.store.pending import CONFIG_FILE, Overlay, PendingChanges

router = APIRouter(prefix="/api/v1", tags=["domains"])

_NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"description": "No domain has this zone"}}
_SAVE_ERRORS: dict[int | str, dict[str, Any]] = {
    400: {"description": "The API token cannot read the zone or its DNS records"},
    409: {"description": "The zone exists already, or an apply is running"},
}


class DomainView(Domain):
    """A domain as the API returns it.

    `cloudflare_off_reason` says why Cloudflare DNS records, DDNS and the route switch are off for
    this domain, and is null when its DNS is on Cloudflare. A save ignores it.
    """

    cloudflare_off_reason: str | None = None


def _view(domain: Domain) -> DomainView:
    return DomainView(**dict(domain), cloudflare_off_reason=cloudflare_off_reason(domain))


class DomainInput(DomainView):
    """A domain to save, with optional new credentials to store under `credentials`.

    Cloudflare takes `api_token`. Any other provider takes `env`, with only the variables that
    `GET /domains/providers` lists for it. Leave both out to keep the stored credentials. A domain
    with no DNS provider has no certificate and takes neither, nor does a self-signed one.
    """

    api_token: RequiredSecretToken | None = None
    env: dict[EnvName, EnvValue] | None = None

    @model_validator(mode="after")
    def _credentials_fit(self) -> Self:
        if self.api_token is None and self.env is None:
            return self
        if self.dns_provider is None or self.dns_provider == SELF_SIGNED:
            raise ValueError("a domain with no DNS provider takes no credentials")
        check_credentials(self.dns_provider, self.api_token, self.env or {})
        return self


def _stored_token(store: Store, body: DomainInput, name: str) -> SecretStr | None:
    """Return the stored Cloudflare token, after checking that the credentials stored under
    `name` fit the body."""
    credential = store.load_secrets().dns_credentials.get(name)
    if credential is None:
        field = "api_token" if body.dns_provider == CLOUDFLARE else "env"
        message = "No credentials are stored under this name."
        raise ApiError(422, "credentials_required", message, field=field)
    if credential.provider != body.dns_provider:
        message = f"These credentials are for {credential.provider}."
        raise ApiError(422, "credentials_provider", message, field="credentials")
    return credential.api_token


def _shared_zones(store: Store, body: DomainInput) -> list[str]:
    """Return the other pending zones that use the credentials name, whose credentials the new
    ones replace. Answer 422 if a live or pending domain, the edited one included, uses the name
    with another provider: `secrets.yaml` changes now, while its config change is only staged."""
    with edit_lock(store) as held:
        pending = _config_tree(store, held)[1]
    users = [
        d for d in (*store.load_config().domains, *pending) if d.credentials == body.credentials
    ]
    if any(d.dns_provider != body.dns_provider for d in users):
        message = "Choose a new credentials name when you switch providers."
        raise ApiError(422, "credentials_in_use", message, field="credentials")
    return [d.zone for d in pending if d.credentials == body.credentials and d.zone != body.zone]


def _check_credentials(services: ServicesDep, store: Store, body: DomainInput) -> None:
    """Answer 4xx unless the credentials fit the provider of each domain that will use them, and
    pass `check_credential_values`. A domain with no DNS provider has no credentials to check."""
    if body.credentials is None:
        return
    if body.api_token is None and body.env is None:
        token, zones = _stored_token(store, body, body.credentials), [body.zone]
    else:
        token, zones = body.api_token, [body.zone, *_shared_zones(store, body)]
    check_credential_values(services, store, token, body.env or {}, zones)


def check_credential_values(
    services: ServicesDep,
    store: Store,
    token: SecretStr | None,
    env: Mapping[str, SecretStr],
    zones: list[str],
) -> None:
    """Answer 4xx unless path variables name files in `/data/secrets` and a Cloudflare token can
    read each of the zones."""
    try:
        check_paths({k: v.get_secret_value() for k, v in env.items()}, store.secrets_dir)
    except ValueError as error:
        raise ApiError(422, "path_outside_secrets", str(error), field="env") from None
    if token is None or services.certificates.lego is None:
        return  # Other providers are checked by the first issuance. Self-signed mode is offline.
    for zone in zones:
        try:
            check_token(zone, token.get_secret_value())
        except TokenError as error:
            raise ApiError(400, "invalid_token", str(error), field="api_token") from None


def _store_credentials(store: Store, body: DomainInput, held: HeldLock) -> None:
    entry: dict[str, object]
    if body.api_token is not None:
        entry = {"provider": CLOUDFLARE, "api_token": body.api_token.get_secret_value()}
    elif body.env is not None:
        env = {name: value.get_secret_value() for name, value in body.env.items()}
        entry = {"provider": body.dns_provider, "env": env}
    else:
        return
    tree = load_tree(store.secrets_path)
    tree.setdefault("dns_credentials", CommentedMap())[body.credentials] = entry
    store.save_secrets(tree, held)


def _domain(body: DomainInput) -> Domain:
    fields = {"api_token", "env", "cloudflare_off_reason"}
    return Domain.model_validate(body.model_dump(exclude=fields))


def _config_tree(store: Store, held: HeldLock) -> tuple[CommentedMap, list[Domain]]:
    tree = PendingChanges(store).read_tree(CONFIG_FILE, held)
    return tree, list(validate_config(tree).domains)


def _index(domains: list[Domain], zone: str) -> int:
    index = next((i for i, domain in enumerate(domains) if domain.zone == zone), None)
    if index is None:
        raise not_found("domain", "zone")
    return index


@router.get("/domains/providers", response_model=list[LegoProvider])
def list_providers() -> list[LegoProvider]:
    """Return the DNS providers of the bundled lego, each with the variables it takes.

    `exec` and `manual` are not offered. Cloudflare takes `api_token` instead of variables.
    """
    return catalog()


@router.get("/domains", response_model=list[DomainView])
def list_domains(runtime: Runtime) -> list[DomainView]:
    """Return every domain, pending edits included."""
    with edit_lock(runtime.store) as held:
        return [_view(domain) for domain in _config_tree(runtime.store, held)[1]]


@router.get("/domains/{zone}", response_model=DomainView, responses=_NOT_FOUND)
def get_domain(zone: str, runtime: Runtime) -> DomainView:
    """Return one domain, pending edits included."""
    with edit_lock(runtime.store) as held:
        domains = _config_tree(runtime.store, held)[1]
    return _view(domains[_index(domains, zone)])


@router.post("/domains", status_code=201, response_model=DomainView, responses=_SAVE_ERRORS)
def create_domain(body: DomainInput, runtime: Runtime, services: ServicesDep) -> DomainView:
    """Check the credentials, then stage a new domain. Its certificate is issued after the next
    apply."""
    store = runtime.store
    _check_credentials(services, store, body)
    domain = _domain(body)
    with edit_lock(store) as held:
        tree, domains = _config_tree(store, held)
        if any(existing.zone == domain.zone for existing in domains):
            raise ApiError(409, "domain_exists", "This zone is configured already.", field="zone")
        tree.setdefault("domains", []).append(domain.model_dump(mode="json", exclude_defaults=True))
        PendingChanges(store).stage_tree(CONFIG_FILE, tree, held)
        _store_credentials(store, body, held)
    return _view(domain)


@router.put("/domains/{zone}", response_model=DomainView, responses=_NOT_FOUND | _SAVE_ERRORS)
def update_domain(
    zone: str, body: DomainInput, runtime: Runtime, services: ServicesDep
) -> DomainView:
    """Check the credentials, then stage the changed domain. The zone cannot change."""
    store = runtime.store
    if body.zone != zone:
        raise ApiError(422, "zone_changed", "Add a new domain to use another zone.", field="zone")
    _check_credentials(services, store, body)
    domain = _domain(body)
    with edit_lock(store) as held:
        tree, domains = _config_tree(store, held)
        index = _index(domains, zone)
        if domains[index] != domain:
            tree["domains"][index] = domain.model_dump(mode="json", exclude_defaults=True)
            PendingChanges(store).stage_tree(CONFIG_FILE, tree, held)
        _store_credentials(store, body, held)
    return _view(domain)


def _routes_using(overlay: Overlay, zone: str) -> list[str]:
    """Return the routes with a host that no other domain covers."""
    others = [domain for domain in overlay.config.domains if domain.zone != zone]
    return sorted(
        {
            route.name
            for route in overlay.routes
            if not isinstance(route, TcpRoute)
            for host in route.hosts
            if in_zone(host, zone) and domain_of(host, others) is None
        }
    )


@router.delete(
    "/domains/{zone}",
    status_code=204,
    response_class=Response,
    responses=_NOT_FOUND | {409: {"description": "Routes use this domain"}},
)
def delete_domain(zone: str, runtime: Runtime) -> Response:
    """Stage the removal of a domain. Refused while a route has a host in it. Its certificate
    is removed after the next apply."""
    store = runtime.store
    pending = PendingChanges(store)
    with edit_lock(store) as held:
        tree, domains = _config_tree(store, held)
        index = _index(domains, zone)
        users = _routes_using(pending.load_overlay(held), zone)
        if users:
            raise ApiError(409, "domain_in_use", f"Routes use this domain: {', '.join(users)}.")
        del tree["domains"][index]
        pending.stage_tree(CONFIG_FILE, tree, held)
    return Response(status_code=204)
