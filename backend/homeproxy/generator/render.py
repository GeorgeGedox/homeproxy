"""Turn validated config and routes into the files of one release.

Rendering is a pure function of its input, with sorted output and no timestamps, so the same config
always gives the same bytes. A LAN/public switch changes only `lan-only.map` and `hsts.map`, and a
redirect target only `redirect.map`, so the apply pipeline can make such a change through the
Runtime API without a reload.
"""

import ipaddress
import os
import shutil
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from jinja2 import Environment, PackageLoader, StrictUndefined
from pydantic import SecretStr

from homeproxy.generator.certificate import DEFAULT_CERTIFICATE_NAME
from homeproxy.generator.cloudflare_ranges import CLOUDFLARE_LIST, DEFAULT_RANGES, ranges_file
from homeproxy.generator.crowdsec import BOUNCER_PAGES, SPOA_SOCKET
from homeproxy.generator.escape import (
    GeneratorError,
    number,
    only_escaped,
    path_prefix,
    quoted,
    quoted_log_format,
    token,
)
from homeproxy.generator.paths import NOT_FOUND_BACKEND, PATHS_MAP, path_entries, path_hosts
from homeproxy.generator.redirect import REDIRECT_CODES, REDIRECT_MAP, redirect_entries
from homeproxy.generator.route_types import (
    BACKEND_PREFIX,
    HTTPS_SOCKET,
    PASSTHROUGH_SOCKET,
    SNI_MAP,
    RouteError,
    sni_entries,
    stream_backend,
    upstream_addresses,
)
from homeproxy.generator.userlist import password_hash
from homeproxy.geoip.country_map import blocked_country_map
from homeproxy.logs.collector import SYSLOG_SOCKET
from homeproxy.models.config import Config
from homeproxy.models.domain import domain_of
from homeproxy.models.fields import duration_ms
from homeproxy.models.route import HttpRoute, PassthroughRoute, RedirectRoute, Route, TcpRoute
from homeproxy.util.atomic import atomic_write, fsync_dir

CERTS_DIR = "/data/certs"
"""The certificates HAProxy loads. Certificates are not part of a release, so the path is fixed."""

SERVER_STATE_FILE = "/data/state/haproxy-server-state"
"""HAProxy reads this file on start. The pipeline writes `show servers state` to it first. HAProxy
reads it after parsing, when `default-path` no longer applies, so the path is absolute."""

USERLISTS_CONFIG = "conf.d/15-userlists.cfg"
"""The release file with the basic auth hashes. It never belongs in a diff the admin sees."""

ALLOW_LIST = "allow.lst"
"""The CrowdSec allowlist. `homeproxy.crowdsec.allowlist` also writes it in the live release."""
_COUNTRY_BLOCK_MAP = "country-block.map"
_COUNTRY_MAP = "country.map"
_CROWDSEC_SPOE = "crowdsec-spoe.cfg"
_HSTS_MAP = "hsts.map"
_HOSTS_MAP = "hosts.map"
_LAN_LIST = "lan.lst"
_LAN_ONLY_MAP = "lan-only.map"
_PLAIN_HTTP_MAP = "plain-http.map"
_PROXIED_ONLY_MAP = "proxied-only.map"
_MAIN_CONFIG = "conf.d/10-haproxy.cfg"
_BACKENDS_CONFIG = "conf.d/20-backends.cfg"
_INCLUDE_PREFIX = "90-include-"

_TCP_CHECK_INTERVAL = "10s"

_MAX_INTERVAL_MS = 2**31 - 1
"""The largest `inter` HAProxy accepts, about 24.8 days."""

_FRAMING_HEADERS = frozenset(
    {
        # These describe the connection or the framing of the message that HAProxy sends.
        "connection",
        "content-length",
        "keep-alive",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
_REJECTED_HEADERS = {
    # HomeProxy sets these for the upstream from the real client, as the frontend found it.
    "request_headers": _FRAMING_HEADERS
    | {"cf-connecting-ip", "x-forwarded-for", "x-forwarded-proto"},
    # These only mean something on a request.
    "response_headers": _FRAMING_HEADERS | {"cf-connecting-ip", "host"},
}


BasicAuth = Mapping[str, Mapping[str, SecretStr]]
"""Basic auth hashes by route name, then by user name, as in `secrets.yaml`."""


@dataclass(frozen=True)
class RenderedRelease:
    """The files of one release, by path relative to the release directory."""

    files: Mapping[str, str]


@dataclass(frozen=True)
class _Server:
    """A server is named after its address. The server state file matches servers by name, so a
    changed address must change the name, or HAProxy keeps the old address after a reload."""

    address: str
    tls_name: str | None


@dataclass(frozen=True)
class _Backend:
    name: str
    websocket: bool
    check_path: str | None
    check_host: str
    check_interval: str
    tls_verify: bool
    strip_prefix: str | None
    request_headers: tuple[tuple[str, str], ...]
    response_headers: tuple[tuple[str, str], ...]
    servers: tuple[_Server, ...]
    sticky: bool
    users: tuple[tuple[str, SecretStr], ...]


def _environment() -> Environment:
    environment = Environment(
        loader=PackageLoader("homeproxy.generator"),
        undefined=StrictUndefined,
        # HTML escaping is wrong for this format. `only_escaped` makes the filters mandatory.
        autoescape=False,  # noqa: S701
        finalize=only_escaped,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    environment.filters.update(
        token=token,
        number=number,
        quoted=quoted,
        quoted_log_format=quoted_log_format,
        path_prefix=path_prefix,
        password_hash=password_hash,
    )
    return environment


ENVIRONMENT: Final[Environment] = _environment()
"""The templates' Jinja2 environment. Every value it writes must pass an escaping filter."""


def _check_interval(route: HttpRoute, interval: str) -> str:
    try:
        milliseconds = duration_ms(interval)
    except ValueError:
        raise RouteError(route.name, "health_check.interval", "not a valid duration") from None
    if milliseconds > _MAX_INTERVAL_MS:
        raise RouteError(route.name, "health_check.interval", "must be at most 24 days")
    return interval


def _sorted_headers(
    route: HttpRoute, field: str, headers: dict[str, str]
) -> tuple[tuple[str, str], ...]:
    """Return the headers of `field`, `request_headers` or `response_headers`, in a fixed order, or
    raise for a name that is not safe in that direction.

    Names compare without case, as HTTP does. With both `X-A` and `x-a`, the last `set-header` would
    win without any message, so that is an error too.
    """
    direction = field.removesuffix("_headers")
    seen: set[str] = set()
    for name in headers:
        lowered = name.lower()
        if lowered in _REJECTED_HEADERS[field]:
            raise RouteError(route.name, field, f"{name} cannot be set as a {direction} header")
        if lowered in seen:
            raise RouteError(route.name, field, f"{name} is set twice with different case")
        seen.add(lowered)
    return tuple(sorted(headers.items(), key=lambda item: item[0].lower()))


def _is_ip_address(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _tls_name(route: HttpRoute, host: str) -> str | None:
    """Return the name for SNI and certificate checks on an upstream, or None without TLS.

    A name in the upstream address is the name its certificate should hold. An IP address says
    nothing about the certificate, so the route's first host is used. Checking no name at all would
    accept any certificate from a public CA, including one for an attacker's own domain.
    """
    if not route.tls_upstream.enabled:
        return None
    return route.hosts[0] if _is_ip_address(host) else host


def _users(route: HttpRoute, basic_auth: BasicAuth) -> tuple[tuple[str, SecretStr], ...]:
    if route.auth.type == "none":
        return ()
    users = basic_auth.get(route.name)
    if not users:
        # HAProxy would refuse every login, which looks like a wrong password to the admin.
        raise RouteError(route.name, "auth", "basic auth needs at least one user in secrets.yaml")
    return tuple(sorted(users.items()))


def _backend(route: HttpRoute, basic_auth: BasicAuth) -> _Backend:
    check = route.health_check
    addresses = upstream_addresses(route)
    servers = [
        _Server(address, _tls_name(route, address.rpartition(":")[0])) for address in addresses
    ]
    return _Backend(
        name=BACKEND_PREFIX + route.name,
        websocket=route.websocket,
        check_path=check.path if check else None,
        check_host=route.hosts[0],
        check_interval=_check_interval(route, check.interval) if check else _TCP_CHECK_INTERVAL,
        tls_verify=route.tls_upstream.verify,
        strip_prefix=route.path_prefix if route.strip_prefix and route.path_prefix != "/" else None,
        request_headers=_sorted_headers(route, "request_headers", route.request_headers),
        response_headers=_sorted_headers(route, "response_headers", route.response_headers),
        servers=tuple(servers),
        sticky=route.sticky,
        users=_users(route, basic_auth),
    )


def _switched_off_hosts(routes: Iterable[Route], enabled: Iterable[Route]) -> list[str]:
    """Return the hosts of disabled routes that no enabled route serves. They answer 404 as known
    hosts, so the CrowdSec scenario does not count their visitors."""
    served = {host for route in enabled if not isinstance(route, TcpRoute) for host in route.hosts}
    off = {
        host
        for route in routes
        if not route.enabled and not isinstance(route, TcpRoute)
        for host in route.hosts
    }
    return sorted(off - served)


def _lines(lines: Iterable[str]) -> str:
    return "".join(f"{line}\n" for line in sorted(lines))


def allow_list(entries: Iterable[str]) -> str:
    """Return `allow.lst` for validated allowlist entries, one per line, sorted."""
    return _lines(token(entry) for entry in entries)


def _backend_flags(routes: Iterable[Route]) -> str:
    """Return a flag map keyed by backend name, so it cannot disagree with `hosts.map` on a host."""
    return _lines(f"{token(BACKEND_PREFIX + route.name)} 1" for route in routes)


def _proxied_only(route: HttpRoute | RedirectRoute | PassthroughRoute, config: Config) -> bool:
    """Return whether Cloudflare proxies every host of a route, as reconcile creates its records.

    Such a route refuses clients that come neither through Cloudflare nor from the LAN. A host with
    a DNS-only record would be reached directly, so one such host leaves the route open. That
    includes the DDNS record, which reconcile never makes a proxied CNAME.
    """
    cloudflare = route.cloudflare
    if not (config.cloudflare.enabled and route.exposure == "public" and cloudflare.dns):
        return False
    for host in route.hosts:
        domain = domain_of(host, config.domains)
        if domain is None or host == domain.ddns_record:
            return False
        proxied = cloudflare.proxied
        if proxied is None:
            proxied = domain.cloudflare.default_proxied
        if not proxied:
            return False
    return True


def _plain_http(route: HttpRoute | RedirectRoute, config: Config) -> bool:
    """Return whether every host of a route is in a domain with no certificate, so the route is
    served over plain HTTP only. The apply pipeline refuses a route that mixes both kinds."""
    domains = [domain_of(host, config.domains) for host in route.hosts]
    return all(domain is not None and not domain.has_certificate for domain in domains)


def _country_blocks(routes: Iterable[Route], config: Config) -> dict[str, set[str]]:
    """Return the countries blocked on each public route, by route name. The global list applies
    to every public route. A LAN-only route already refuses every outside client. A route list
    that is off blocks nothing. A route list that replaces the global list stands alone."""
    blocked = config.geoip.block_countries
    return {
        route.name: _route_blocks(route, blocked) for route in routes if route.exposure == "public"
    }


def _route_blocks(route: Route, blocked: Iterable[str]) -> set[str]:
    own = route.geoip
    if not own.enabled:
        return set(blocked)
    return set(own.block_countries) if own.mode == "replace" else {*blocked, *own.block_countries}


def _country_map(text: str, blocked: set[str]) -> str:
    """Return the part of `country.map` that HAProxy needs. The saved map's check reads only the
    characters of each line, so a line that is still no network fails here."""
    try:
        return blocked_country_map(text, blocked)
    except (OSError, ValueError):
        raise GeneratorError("country.map is not valid") from None


def _include_files(includes: Mapping[str, str]) -> dict[str, str]:
    files = {}
    for name, text in includes.items():
        # A `/` would write outside `conf.d/`.
        if "/" in name:
            raise GeneratorError("an include name holds a character that is not allowed")
        files[f"conf.d/{_INCLUDE_PREFIX}{token(name)}.cfg"] = text
    return files


def render(
    config: Config,
    routes: Iterable[Route],
    includes: Mapping[str, str] | None = None,
    basic_auth: BasicAuth | None = None,
    cloudflare_ranges: Iterable[str] = DEFAULT_RANGES,
    country_map: str = "",
) -> RenderedRelease:
    """Render every file of a release, without touching the disk.

    `routes` are the routes from `routes/*.yaml`. Rules that need more than one file, such as
    duplicate hosts, belong to the apply pipeline and are not checked here. A disabled route has no
    backend, and its hosts answer 404 as known hosts. A `passthrough` route adds the SNI layer in
    front of the HTTPS frontend, and `sni.map`. The HTTPS frontend answers 421 for its hosts, so a
    browser that reuses a connection for one of them connects again. A `tcp` route listens on its
    own port. A `redirect` route has no backend: its hosts answer with the redirect in
    `redirect.map`, over HTTPS and plain HTTP alike. Exposure and GeoIP apply to every route type
    through the same maps, keyed by backend. A host with `http` routes on different paths is in
    `paths.map` instead of `hosts.map`. An `http` or `redirect` route on a domain with no
    certificate is in `plain-http.map`: it is served on the HTTP port only, with no HTTPS redirect
    and no HSTS, and its hosts answer 404 on the HTTPS port. A host that no route serves answers
    404 from `be_unknown_host`, which the CrowdSec scenario in `docs/crowdsec.md` counts.

    `includes` maps the name of each hand-written `includes/<name>.cfg` to its text. Each one is
    copied unchanged to `conf.d/90-include-<name>.cfg`, so HAProxy loads it after the generated
    files and `haproxy -c` checks it with them. Its text is the admin's own HAProxy config, so it is
    not escaped.

    `basic_auth` is `basic_auth` from `secrets.yaml`. Each enabled route with `basic` auth gets a
    userlist of its users in `conf.d/15-userlists.cfg`, which only that file holds. The file exists
    only when such a route does.

    `cloudflare_ranges` are the sources trusted to name the client in `CF-Connecting-IP`, as
    `load_ranges` returns them. A public route that Cloudflare proxies goes in `proxied-only.map`.

    `country_map` is the text of `country.map`, as `load_country_map` returns it. The release
    keeps only what HAProxy needs to find the countries blocked on some public route, so it is
    empty when none is. `country-block.map` holds the countries blocked on each public route.

    With CrowdSec enabled, the HTTPS frontend asks the bouncer about each client outside `lan.lst`
    and `allow.lst`, through `crowdsec-spoe.cfg`, and so do the TCP frontends for `passthrough` and
    `tcp` routes. `allow.lst` holds `crowdsec.allowlist`.

    With `prometheus` enabled, HAProxy serves its own metrics at `/metrics` on `prometheus.listen`,
    to sources in `lan.lst` only.

    Raises `GeneratorError` for a value that is not safe to write, and its subclass `RouteError` for
    a route that passes its model but that HAProxy would reject or that would break its responses.
    """
    routes = tuple(routes)
    enabled = sorted((route for route in routes if route.enabled), key=lambda item: item.name)
    http_routes = [route for route in enabled if isinstance(route, HttpRoute)]
    passthrough = [route for route in enabled if isinstance(route, PassthroughRoute)]
    redirects = [route for route in enabled if isinstance(route, RedirectRoute)]
    named: list[HttpRoute | RedirectRoute] = [*http_routes, *redirects]
    plain_http = {route.name for route in named if _plain_http(route, config)}
    streams = [
        stream_backend(route) for route in enabled if isinstance(route, PassthroughRoute | TcpRoute)
    ]

    network = config.network
    # Captcha answers and AppSec both need the request body.
    crowdsec_body = config.crowdsec.captcha_provider != "none" or config.crowdsec.appsec
    main_config = ENVIRONMENT.get_template("10-haproxy.cfg.j2").render(
        http_port=network.http_port,
        https_port=network.https_port,
        default_https_port=network.https_port == 443,
        certs_dir=f"{CERTS_DIR}/",
        default_certificate=f"{CERTS_DIR}/{DEFAULT_CERTIFICATE_NAME}",
        server_state_file=SERVER_STATE_FILE,
        backend_prefix=BACKEND_PREFIX,
        syslog_socket=SYSLOG_SOCKET,
        crowdsec=config.crowdsec.enabled,
        crowdsec_body=crowdsec_body,
        crowdsec_syslog=config.crowdsec.syslog if config.crowdsec.enabled else None,
        bouncer_pages=BOUNCER_PAGES,
        spoa_socket=SPOA_SOCKET,
        sni_layer=bool(passthrough),
        https_socket=HTTPS_SOCKET,
        passthrough_socket=PASSTHROUGH_SOCKET,
        proxied_passthrough=[
            BACKEND_PREFIX + route.name for route in passthrough if _proxied_only(route, config)
        ],
        tcp_backends=[backend for backend in streams if backend.listen_port is not None],
        prometheus_listen=config.prometheus.listen if config.prometheus.enabled else None,
        redirect_codes=REDIRECT_CODES,
    )
    backends = [_backend(route, basic_auth or {}) for route in http_routes]
    backends_config = ENVIRONMENT.get_template("20-backends.cfg.j2").render(
        backends=backends, stream_backends=streams, tcp_check_interval=_TCP_CHECK_INTERVAL
    )
    paths = path_hosts(http_routes)
    blocks = _country_blocks(enabled, config)
    hosts_map = _lines(
        [
            *(
                f"{token(host)} {token(BACKEND_PREFIX + route.name)}"
                for route in named
                for host in route.hosts
                if host not in paths
            ),
            *(
                f"{token(host)} {NOT_FOUND_BACKEND}"
                for host in _switched_off_hosts(routes, enabled)
            ),
        ]
    )
    files = {
        _MAIN_CONFIG: main_config,
        _BACKENDS_CONFIG: backends_config,
        _HOSTS_MAP: hosts_map,
        PATHS_MAP: _lines(path_entries(http_routes, paths)),
        _LAN_ONLY_MAP: _backend_flags(r for r in enabled if r.exposure == "lan"),
        _HSTS_MAP: _backend_flags(
            r for r in http_routes if r.exposure == "public" and r.name not in plain_http
        ),
        _PLAIN_HTTP_MAP: _backend_flags(r for r in named if r.name in plain_http),
        _PROXIED_ONLY_MAP: _backend_flags(r for r in named if _proxied_only(r, config)),
        REDIRECT_MAP: _lines(redirect_entries(redirects)),
        CLOUDFLARE_LIST: ranges_file(cloudflare_ranges),
        _LAN_LIST: _lines(token(cidr) for cidr in network.lan_cidrs),
        ALLOW_LIST: allow_list(config.crowdsec.allowlist),
        _COUNTRY_MAP: _country_map(country_map, set().union(*blocks.values())),
        _COUNTRY_BLOCK_MAP: _lines(
            f"{token(BACKEND_PREFIX + name)}/{token(country)} 1"
            for name, countries in blocks.items()
            for country in countries
        ),
        **_include_files(includes or {}),
    }
    if passthrough:
        files[SNI_MAP] = _lines(sni_entries(passthrough))
    if config.crowdsec.enabled:
        spoe = ENVIRONMENT.get_template("crowdsec-spoe.cfg.j2")
        files[_CROWDSEC_SPOE] = spoe.render(crowdsec_body=crowdsec_body)
    if locked := [backend for backend in backends if backend.users]:
        files[USERLISTS_CONFIG] = ENVIRONMENT.get_template("15-userlists.cfg.j2").render(
            backends=locked
        )
    return RenderedRelease(files=files)


def write_release(release: RenderedRelease, release_dir: Path) -> None:
    """Write a release to the new directory `release_dir`, which appears complete or not at all.

    The parent directory must exist. An existing `release_dir` raises `FileExistsError`, because a
    release is never rewritten. Only runtime updates change a live release's files in place:
    `allow.lst` by an allowlist edit and `cloudflare.lst` by the range refresh.
    """
    if release_dir.exists():
        raise FileExistsError(f"release directory already exists: {release_dir.name}")
    temp_dir = Path(tempfile.mkdtemp(dir=release_dir.parent, prefix=f".{release_dir.name}."))
    try:
        temp_dir.chmod(0o755)
        for relative_path, content in release.files.items():
            path = temp_dir / relative_path
            path.parent.mkdir(exist_ok=True)
            # Generated files are ASCII, because the filters allow nothing else. Includes may not.
            # The userlists hold password hashes. HAProxy runs as the user that writes them.
            mode = 0o600 if relative_path == USERLISTS_CONFIG else 0o644
            atomic_write(path, content.encode("utf-8"), mode=mode)
        fsync_dir(temp_dir)
        os.rename(temp_dir, release_dir)
    except BaseException:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    fsync_dir(release_dir.parent)
