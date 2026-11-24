"""Credential endpoints: list, replace and remove the DNS credentials in `secrets.yaml`.

A read masks every value whose name marks a secret (`SECRET_NAME`), whatever its length, and a
Cloudflare token always. Redaction hides only some of those, so nothing shown is a value the logs
hide. Writes reach `secrets.yaml` at once, because secrets have no pending copy, so they are
checked against every live and pending domain that uses the name. A new name comes with a domain
save. The audit log records only the name.
"""

import logging
from typing import Any

from fastapi import APIRouter, Response
from pydantic import BaseModel, Field

from homeproxy.api.apply import ServicesDep
from homeproxy.api.changes import edit_lock
from homeproxy.api.domains import check_credential_values
from homeproxy.api.errors import ApiError
from homeproxy.auth.routes import Runtime
from homeproxy.certs.providers import CLOUDFLARE
from homeproxy.models import Domain, StrictModel
from homeproxy.models.fields import RequiredSecretToken
from homeproxy.models.secrets import (
    SECRET_NAME,
    DnsCredential,
    EnvName,
    EnvValue,
    check_credentials,
)
from homeproxy.store import HeldLock, Store, load_tree, validate_config
from homeproxy.store.pending import CONFIG_FILE, PendingChanges

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["credentials"])

_NOT_FOUND: dict[int | str, dict[str, Any]] = {
    404: {"description": "No credentials are stored under this name"}
}


class CredentialView(BaseModel):
    """Stored credentials. A variable's value is null when it is a secret: it is set, and never
    returned. `zones` are the live and pending domains that use the name."""

    name: str
    provider: str
    variables: dict[str, str | None]
    zones: list[str]


class CredentialInput(StrictModel):
    """The new values of stored credentials. The provider cannot change.

    Cloudflare takes `api_token`; leave it out to keep the stored token. Any other provider takes
    `env` with the variables `GET /domains/providers` lists for it: each one maps to its new value,
    or to null to keep the stored value. A variable left out is removed.
    """

    api_token: RequiredSecretToken | None = None
    env: dict[EnvName, EnvValue | None] = Field(default_factory=dict)


def _domains(store: Store, held: HeldLock) -> list[Domain]:
    """Return the live and pending domains. A domain in both is listed twice."""
    pending = validate_config(PendingChanges(store).read_tree(CONFIG_FILE, held)).domains
    return [*store.load_config().domains, *pending]


def _zones(domains: list[Domain], name: str) -> list[str]:
    return sorted({domain.zone for domain in domains if domain.credentials == name})


def _view(name: str, credential: DnsCredential, domains: list[Domain]) -> CredentialView:
    if credential.api_token is not None:
        variables: dict[str, str | None] = {"api_token": None}
    else:
        values = {n: v.get_secret_value() for n, v in credential.env.items()}
        variables = {n: None if SECRET_NAME.search(n) else v for n, v in values.items()}
    return CredentialView(
        name=name, provider=credential.provider, variables=variables, zones=_zones(domains, name)
    )


def _stored(store: Store, name: str) -> DnsCredential:
    credential = store.load_secrets().dns_credentials.get(name)
    if credential is None:
        raise ApiError(404, "credentials_not_found", "No credentials are stored under this name.")
    return credential


@router.get("/credentials", response_model=list[CredentialView])
def list_credentials(runtime: Runtime) -> list[CredentialView]:
    """Return the stored credentials with their non-secret values and the zones that use them."""
    store = runtime.store
    with edit_lock(store) as held:
        domains = _domains(store, held)
    credentials = store.load_secrets().dns_credentials
    return [_view(name, credentials[name], domains) for name in sorted(credentials)]


@router.put(
    "/credentials/{name}",
    response_model=CredentialView,
    responses=_NOT_FOUND
    | {
        400: {"description": "The API token cannot read a zone that uses the name"},
        422: {"description": "The values do not fit the provider. No value is echoed"},
    },
)
def replace_credential(
    name: str, body: CredentialInput, runtime: Runtime, services: ServicesDep
) -> CredentialView:
    """Check the new values against each zone that uses the name, then store them."""
    store = runtime.store
    stored = _stored(store, name)
    field = "api_token" if stored.provider == CLOUDFLARE else "env"
    if any(v is None and n not in stored.env for n, v in body.env.items()):
        message = "A variable sent as null has no stored value to keep."
        raise ApiError(422, "credentials_not_stored", message, field="env")
    env = {n: stored.env[n] if v is None else v for n, v in body.env.items()}
    token = body.api_token or stored.api_token
    if token is None and not env:
        message = "Send the new values. A replace with none would erase the credentials."
        raise ApiError(422, "credentials_empty", message, field=field)
    try:
        check_credentials(stored.provider, token, env)
    except ValueError as error:
        raise ApiError(422, "credentials_provider", str(error), field=field) from None
    with edit_lock(store) as held:
        domains = _domains(store, held)
    # A kept token passed this check when it was stored, so only a new one is checked.
    check_credential_values(services, store, body.api_token, env, _zones(domains, name))
    entry: dict[str, object] = {"provider": stored.provider}
    if token is not None:
        entry["api_token"] = token.get_secret_value()
    else:
        entry["env"] = {n: v.get_secret_value() for n, v in env.items()}
    with edit_lock(store) as held:
        tree = load_tree(store.secrets_path)
        tree["dns_credentials"][name] = entry
        store.save_secrets(tree, held)
    logger.info("credentials %s replaced", name)
    store.audit.record("admin", "credentials.replace", {"name": name})
    return _view(name, _stored(store, name), domains)


@router.delete(
    "/credentials/{name}",
    status_code=204,
    response_class=Response,
    responses=_NOT_FOUND | {409: {"description": "Domains use these credentials"}},
)
def delete_credential(name: str, runtime: Runtime) -> Response:
    """Remove credentials that no live or pending domain uses."""
    store = runtime.store
    _stored(store, name)
    with edit_lock(store) as held:
        zones = _zones(_domains(store, held), name)
        if zones:
            message = f"Domains use these credentials: {', '.join(zones)}."
            raise ApiError(409, "credentials_in_use", message)
        tree = load_tree(store.secrets_path)
        del tree["dns_credentials"][name]
        store.save_secrets(tree, held)
    logger.info("credentials %s removed", name)
    store.audit.record("admin", "credentials.delete", {"name": name})
    return Response(status_code=204)
