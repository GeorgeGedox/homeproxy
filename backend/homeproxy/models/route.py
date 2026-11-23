"""Models for `routes/<name>.yaml`. The `type` field selects the model.

Each model forbids unknown keys, so a field that does not apply to a route type fails validation,
for example `auth` on a `passthrough` route. Field order matches the order in a route file.

Rules that need more than one file live in the apply pipeline, not here: hosts inside a configured
domain, routes that share a host, port clashes, and the dashboard as an upstream.
"""

from typing import Annotated, Any, Literal, Self, cast

from pydantic import (
    AfterValidator,
    Field,
    TypeAdapter,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_core import InitErrorDetails, PydanticCustomError

from homeproxy.models.base import MODEL_CONFIG, StrictModel
from homeproxy.models.fields import (
    CountryCode,
    DnsTarget,
    Duration,
    FileVersion,
    HeaderName,
    HeaderValue,
    Hostname,
    HostPort,
    HttpPath,
    Name,
    PathPrefix,
    Port,
    RedirectCode,
    RedirectUrl,
    unique,
)

Exposure = Literal["lan", "public"]
RouteHosts = Annotated[list[Hostname], Field(min_length=1), AfterValidator(unique)]


class Upstream(StrictModel):
    """One backend server."""

    address: HostPort


class HealthCheck(StrictModel):
    """An HTTP health check against the upstream."""

    path: HttpPath
    interval: Duration = "10s"


class TlsUpstream(StrictModel):
    """How HAProxy connects to the upstream."""

    enabled: bool = False
    verify: bool = True


class RouteAuth(StrictModel):
    """Client authentication. Hashes for `basic` are in `secrets.yaml`, never in a route file."""

    type: Literal["none", "basic"] = "none"


class HttpRouteCloudflare(StrictModel):
    """The per-route Cloudflare switch. It has no effect while the integration is off.

    An unset `proxied` means the zone's `default_proxied`. A plain false default would give a
    hand-written public route a DNS-only CNAME, which exposes the home WAN IP.
    """

    dns: bool = False
    proxied: bool | None = None


class PassthroughRouteCloudflare(StrictModel):
    """The per-route Cloudflare switch. Proxying would end TLS at Cloudflare, so it starts off."""

    dns: bool = False
    proxied: bool = False


class RouteDns(StrictModel):
    """The answer the internal DNS server gives for this route's hosts."""

    target: DnsTarget = "haproxy"


class RouteGeoip(StrictModel):
    """Countries blocked on this route, added to the global list. While `enabled` is false the list
    is kept but blocks nothing."""

    enabled: bool = True
    block_countries: list[CountryCode] = Field(default_factory=list)


class HttpRoute(StrictModel):
    """HAProxy terminates TLS with the domain's certificate and routes by Host header and path.

    Routes on one host differ by `path_prefix`, and the longest prefix that ends on a segment
    boundary wins. `strip_prefix` removes it from the path the upstream sees.
    """

    version: FileVersion
    name: Name
    type: Literal["http"]
    hosts: RouteHosts
    exposure: Exposure
    upstreams: list[Upstream] = Field(min_length=1)
    health_check: HealthCheck | None = None
    tls_upstream: TlsUpstream = Field(default_factory=TlsUpstream)
    websocket: bool = False
    auth: RouteAuth = Field(default_factory=RouteAuth)
    request_headers: dict[HeaderName, HeaderValue] = Field(default_factory=dict)
    response_headers: dict[HeaderName, HeaderValue] = Field(default_factory=dict)
    path_prefix: PathPrefix = "/"
    strip_prefix: bool = False
    cloudflare: HttpRouteCloudflare = Field(default_factory=HttpRouteCloudflare)
    dns: RouteDns = Field(default_factory=RouteDns)
    geoip: RouteGeoip = Field(default_factory=RouteGeoip)
    enabled: bool = True


class PassthroughRoute(StrictModel):
    """HAProxy reads the SNI name and forwards the encrypted stream to the upstream."""

    version: FileVersion
    name: Name
    type: Literal["passthrough"]
    hosts: RouteHosts
    exposure: Exposure
    upstreams: list[Upstream] = Field(min_length=1)
    cloudflare: PassthroughRouteCloudflare = Field(default_factory=PassthroughRouteCloudflare)
    dns: RouteDns = Field(default_factory=RouteDns)
    geoip: RouteGeoip = Field(default_factory=RouteGeoip)
    enabled: bool = True


class TcpRoute(StrictModel):
    """HAProxy listens on its own port and forwards raw TCP to the upstream."""

    version: FileVersion
    name: Name
    type: Literal["tcp"]
    listen_port: Port
    exposure: Exposure
    upstreams: list[Upstream] = Field(min_length=1)
    geoip: RouteGeoip = Field(default_factory=RouteGeoip)
    enabled: bool = True


class Redirect(StrictModel):
    """Where a redirect route sends every request. `keep_path` appends the request's path and query
    to `to`."""

    to: RedirectUrl
    code: RedirectCode = 301
    keep_path: bool = False

    @model_validator(mode="after")
    def _no_query_kept(self) -> Self:
        if not self.keep_path or "?" not in self.to:
            return self
        error = PydanticCustomError(
            "query_kept", "must have no query when the path and query are kept"
        )
        # Raised as a ValidationError so the error names `to`, the field the editor shows.
        details = InitErrorDetails(type=error, loc=("to",), input=self.to)
        raise ValidationError.from_exception_data(type(self).__name__, [details])


class RedirectRoute(StrictModel):
    """HAProxy answers every request on the hosts with a redirect, and forwards nothing."""

    version: FileVersion
    name: Name
    type: Literal["redirect"]
    hosts: RouteHosts
    exposure: Exposure
    redirect: Redirect
    cloudflare: HttpRouteCloudflare = Field(default_factory=HttpRouteCloudflare)
    dns: RouteDns = Field(default_factory=RouteDns)
    geoip: RouteGeoip = Field(default_factory=RouteGeoip)
    enabled: bool = True

    @field_validator("redirect")
    @classmethod
    def _no_loop(cls, value: Redirect, info: ValidationInfo) -> Redirect:
        """Refuse a target on one of the route's own hosts. Plain HTTP moves to HTTPS there, and
        the redirect fires again, so it always loops."""
        host = value.to.partition("://")[2].partition("/")[0].partition(":")[0].lower()
        if host not in {own.lower() for own in info.data.get("hosts", [])}:
            return value
        error = PydanticCustomError("redirect_loop", "must not point at one of the route's hosts")
        # Raised as a ValidationError so the error names `redirect.to`, not the whole `redirect`.
        details = InitErrorDetails(type=error, loc=("to",), input=value.to)
        raise ValidationError.from_exception_data(cls.__name__, [details])


Route = Annotated[
    HttpRoute | PassthroughRoute | TcpRoute | RedirectRoute, Field(discriminator="type")
]

ROUTE_ADAPTER: TypeAdapter[Route] = TypeAdapter(cast(Any, Route), config=MODEL_CONFIG)


def parse_route(data: object) -> HttpRoute | PassthroughRoute | TcpRoute | RedirectRoute:
    """Validate the data of one route file and return the model for its `type`.

    Raises `pydantic.ValidationError`. Pass it through `homeproxy.models.safe_errors` before it
    reaches a log or a response. The first part of each error location is the route type, as in
    `("http", "hosts", 0)`.
    """
    return ROUTE_ADAPTER.validate_python(data)


def route_label(route: Route) -> str:
    """Return what the user sees of a route: its hosts and path prefix, or `port <n>` for tcp."""
    if isinstance(route, TcpRoute):
        return f"port {route.listen_port}"
    path = route.path_prefix if isinstance(route, HttpRoute) and route.path_prefix != "/" else ""
    return ", ".join(route.hosts) + path
