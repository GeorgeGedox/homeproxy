"""`POST /setup`: the setup wizard writes the first `config.yaml` and applies it.

The wizard sends only the token and the password. A detected value fills in each network value
the request leaves out, and a sent value wins. A default HTTP or HTTPS port that another program
listens on must be sent, or the first apply would fail. The client's own address is added to the LAN
ranges when it is outside them, so the admin can reach the dashboard right after setup. The
dashboard keeps the address it listens on now; Settings changes it later.

Setup ends with the first apply, so nothing is pending afterwards and HAProxy leaves the
placeholder page. A failed apply does not fail setup: it is audited like any other and shows as
`last_apply` on `/status`.

The first domain is not part of setup. The wizard adds it through the domains API after the first
login, because adding a domain checks its DNS token and starts certificate issuance.
"""

import logging
import os

from fastapi import APIRouter, Response
from pydantic import Field, SecretStr
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.scalarstring import DoubleQuotedScalarString

from homeproxy.api.apply import ServicesDep, apply
from homeproxy.api.auth import new_password_hash
from homeproxy.auth.errors import AuthError
from homeproxy.auth.routes import ClientAddress, Runtime
from homeproxy.auth.server import bootstrap_listen
from homeproxy.auth.setup_detection import (
    DetectedDnsServer,
    DetectedNetwork,
    DetectedSetup,
    detect_host_setup,
    taken_ports,
)
from homeproxy.auth.sources import lan_policy, parse_address
from homeproxy.models import StrictModel
from homeproxy.models.config import DnsServer, Network
from homeproxy.models.fields import IpOrCidr, IPv4, ListenAddress
from homeproxy.store import StoreError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["setup"])

FORWARD_RESOLVERS = ["1.1.1.1"]
"""Where the internal DNS server sends other names in the zones until the admin picks resolvers.
It is the public resolver HomeProxy already asks for certificate checks, so it adds no new party.
A resolver from the host could be AdGuard, and forwarding to it would loop."""


class SetupNetwork(Network):
    """`network` of the setup request. A value left out is detected."""

    lan_cidrs: list[IpOrCidr] | None = Field(default=None, min_length=1)  # type: ignore[assignment]
    haproxy_lan_ip: IPv4 | None = None  # type: ignore[assignment]


class SetupDnsServer(DnsServer):
    """`dns_server` of the setup request. A listen address left out is detected."""

    listen: ListenAddress | None = None  # type: ignore[assignment]


class SetupRequest(StrictModel):
    """What the setup wizard sends: the token from the container log, the password, and any
    network value to use instead of the detected one."""

    token: SecretStr
    password: SecretStr
    network: SetupNetwork = Field(default_factory=SetupNetwork)
    dns_server: SetupDnsServer = Field(default_factory=SetupDnsServer)


def with_client(lan_cidrs: list[str], peer: str) -> list[str]:
    """Return `lan_cidrs`, plus the client's own address when it is outside them."""
    address = parse_address(peer)
    if address is None or lan_policy(lan_cidrs).allows(peer):
        return lan_cidrs
    return [*lan_cidrs, str(address)]


def setup_values(body: SetupRequest, detected: DetectedSetup, peer: str) -> DetectedSetup:
    """Return the network values setup writes: each sent value, else the detected one."""
    lan_cidrs = body.network.lan_cidrs or detected.network.lan_cidrs
    return DetectedSetup(
        network=DetectedNetwork(
            lan_cidrs=with_client(lan_cidrs, peer) if lan_cidrs else None,
            haproxy_lan_ip=body.network.haproxy_lan_ip or detected.network.haproxy_lan_ip,
        ),
        dns_server=DetectedDnsServer(listen=body.dns_server.listen or detected.dns_server.listen),
    )


def first_config(
    body: SetupRequest, values: DetectedSetup, listen: str, password_hash: str
) -> CommentedMap:
    """Return the first `config.yaml`: the dashboard's current address, the network values setup
    fills in, the DNS resolvers, the other values the request sent, and the password hash."""
    tree = CommentedMap()
    tree["version"] = 1
    tree["dashboard"] = {
        "listen": listen,
        "password_hash": DoubleQuotedScalarString(password_hash),
    }
    tree["network"] = body.network.model_dump(mode="json", exclude_unset=True) | {
        "lan_cidrs": values.network.lan_cidrs,
        "haproxy_lan_ip": values.network.haproxy_lan_ip,
    }
    tree["dns_server"] = {
        "listen": values.dns_server.listen,
        "forward_unknown_to": FORWARD_RESOLVERS,
    } | body.dns_server.model_dump(mode="json", exclude_unset=True, exclude={"listen"})
    return tree


@router.post(
    "/setup",
    status_code=204,
    response_class=Response,
    responses={
        403: {"description": "Wrong setup token, or the client is not on a private address"},
        409: {"description": "Setup has already run"},
        422: {
            "description": "A field is not valid, or not sent and not detected; `field` names it"
        },
        429: {"description": "Too many wrong tokens; see Retry-After"},
    },
)
def setup(
    body: SetupRequest, runtime: Runtime, peer: ClientAddress, services: ServicesDep
) -> Response:
    """Finish setup: check the token, fill in the network values, write `config.yaml`, then run
    the first apply.

    Needs no session. The dashboard keeps running on the address it is on, which setup writes as
    `dashboard.listen`. A failure before the write leaves no file behind, and the same token works
    again. Once the file is written, setup has succeeded whatever the apply does.
    """
    with runtime.setup_claim(peer, body.token.get_secret_value()):
        values = setup_values(body, detect_host_setup(), peer)
        missing = values.missing()
        if missing:
            raise AuthError(
                422,
                "validation_failed",
                f"{', '.join(missing)}: HomeProxy cannot detect this value. Send it.",
                field=missing[0],
            )
        sent = {f"network.{name}" for name in body.network.model_fields_set}
        taken = [path for path in taken_ports() if path not in sent]
        if taken:
            raise AuthError(
                422,
                "validation_failed",
                f"{', '.join(taken)}: another program listens on this port. Send a free one.",
                field=taken[0],
            )
        host, port = bootstrap_listen(os.environ)
        password_hash = new_password_hash(body.password, "password")
        tree = first_config(body, values, f"{host}:{port}", password_hash)
        with runtime.store.lock() as held:
            runtime.store.save_config(tree, held)
    try:
        apply(services)
    except (OSError, StoreError) as error:
        # The pipeline returns its failures as a result. This is what it lets through, such as an
        # unreadable lock file, which leaves no audit entry.
        logger.error("the first apply after setup could not run: %s", error)
    return Response(status_code=204)
