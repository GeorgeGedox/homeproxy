"""Rules that need more than one file, checked by the apply pipeline before it renders.

The models check each file alone. These rules compare files with each other, with `secrets.yaml`
and with the dashboard. Each failure is a short message that is safe to show: it names routes,
hosts, zones and ports, never a secret.
"""

import errno
import ipaddress
import os
import socket
from collections.abc import Callable, Iterable, Mapping, Sequence
from functools import cache
from itertools import combinations
from pathlib import Path

from homeproxy.auth.server import ServerConfigError, bind_error, bootstrap_listen
from homeproxy.auth.sources import PRIVATE_RANGES
from homeproxy.certs.providers import CLOUDFLARE
from homeproxy.geoip.data import GeoipData
from homeproxy.models import (
    Config,
    HttpRoute,
    RedirectRoute,
    Route,
    Secrets,
    TcpRoute,
    route_label,
)
from homeproxy.models.config import Crowdsec
from homeproxy.models.domain import domain_of, in_zone
from homeproxy.models.secrets import SecretsCrowdsec
from homeproxy.store.pending import Overlay


def check_rules(overlay: Overlay, load_secrets: Callable[[], Secrets]) -> list[str]:
    """Return every cross-file rule the overlay breaks, or an empty list.

    `load_secrets` is called only when a rule needs `secrets.yaml`, so an install with no domain
    and no keyed integration needs no secrets file. Its errors propagate.
    """
    config, routes = overlay.config, overlay.routes
    return [
        *_file_names(overlay.route_files),
        *_hosts(config, routes),
        *_plain_http_hosts(config, routes),
        *_cloudflare_hosts(config, routes),
        *_zone_names(config),
        *port_clashes(config, routes),
        *_dashboard_upstreams(config, routes),
        *_secrets(config, cache(load_secrets)),
    ]


_PUBLIC_RANGE_EFFECTS = {
    "network.lan_cidrs": "its clients count as LAN",
    "crowdsec.allowlist": "CrowdSec never checks its clients",
}


def public_ranges(field: str, cidrs: Iterable[str]) -> list[str]:
    """Return a warning for each entry in `cidrs` that reaches outside the private ranges.

    `field` is `network.lan_cidrs` or `crowdsec.allowlist`. A single allowlist address is no
    warning: allowlisting one public host, such as an unbanned client, is its normal use.
    """
    warnings = []
    for cidr in cidrs:
        network = ipaddress.ip_network(cidr)
        single = network.num_addresses == 1
        if (single and field == "crowdsec.allowlist") or _is_private(network):
            continue
        kind = "address" if single else "range"
        warnings.append(f"{field}: {cidr} is a public {kind}, so {_PUBLIC_RANGE_EFFECTS[field]}")
    return warnings


def country_warnings(config: Config, routes: Iterable[Route], data: GeoipData | None) -> list[str]:
    """Return a warning for blocked countries that the GeoIP data does not have, or for a block
    list with no data. Saves refuse new codes like these, but a hand edit or new data can still
    leave some. Failing the apply over them would block unrelated changes."""
    lists = (r.geoip.block_countries for r in routes if r.geoip.enabled)
    blocked = {*config.geoip.block_countries}.union(*lists)
    if not blocked:
        return []
    if data is None:
        return ["countries are blocked, but there is no GeoIP data: see Security → GeoIP"]
    missing = sorted(blocked - set(data.countries))
    if not missing:
        return []
    return [f"not in the GeoIP data, so not blocked: {', '.join(missing)}"]


def path_only_hosts(routes: Iterable[Route]) -> dict[str, str]:
    """Return a warning, keyed by host, for each host whose enabled routes all serve a path below
    `/`. The internal DNS server answers the whole name with HAProxy, so on the LAN every other path
    on it gets 404, while outside it may still reach a public site. A DNS target cannot help: it
    sets the answer for the whole host."""
    prefixes: dict[str, list[str]] = {}
    for route in routes:
        # Only http routes share a host. Another DNS target sends the LAN past HAProxy.
        if not isinstance(route, HttpRoute) or not route.enabled or route.dns.target != "haproxy":
            continue
        for host in route.hosts:
            prefixes.setdefault(host, []).append(route.path_prefix)
    return {
        host: f"{host} is routed only on {', '.join(sorted(paths))}, so on the LAN its other "
        "paths get 404. Add a route for /, or move these paths to their own host."
        for host, paths in prefixes.items()
        if "/" not in paths
    }


def _is_private(network: ipaddress.IPv4Network | ipaddress.IPv6Network) -> bool:
    return any(
        network.version == private.version and network.subnet_of(private)  # type: ignore[arg-type]
        for private in PRIVATE_RANGES
    )


def public_range_warnings(config: Config) -> list[str]:
    """Return the warnings for public ranges in `network.lan_cidrs` and `crowdsec.allowlist`."""
    return [
        *public_ranges("network.lan_cidrs", config.network.lan_cidrs),
        *public_ranges("crowdsec.allowlist", config.crowdsec.allowlist),
    ]


def prometheus_warnings(config: Config) -> list[str]:
    """Return a warning when the exporter's address is not on this host. HAProxy binds it anyway,
    so the exporter can wait for an address that is not up yet, and a typo would go unnoticed."""
    listen = config.prometheus.listen
    if not config.prometheus.enabled or listen is None:
        return []
    host = listen.rpartition(":")[0]
    # Port 0: HAProxy holds the exporter's own port after an apply.
    error = bind_error(host, 0)
    if error is None or error.errno != errno.EADDRNOTAVAIL:
        return []
    return [
        f"prometheus.listen: this host has no address {host} yet, so the exporter answers only "
        "once it has"
    ]


def _file_names(route_files: Mapping[str, Route]) -> list[str]:
    return [
        f"{path}: the route name must be {Path(path).stem!r}"
        for path, route in route_files.items()
        if route.name != Path(path).stem
    ]


HOST_SETTINGS = {
    "exposure": "exposure",
    "cloudflare": "Cloudflare DNS setting",
    "dns": "internal DNS answer",
}
"""Settings that belong to a host, so every route on it must agree on them, with their words."""


def host_group(routes: Sequence[Route], host: str) -> list[Route]:
    """Return the routes that must agree on the settings of `host`: the routes on it, and the
    `http` routes joined to them through shared hosts. Only `http` routes share a host, so any
    other route on `host` stands alone."""
    http = [route for route in routes if isinstance(route, HttpRoute)]
    hosts = {host}
    while joined := {h for route in http if hosts & {*route.hosts} for h in route.hosts} - hosts:
        hosts |= joined
    return [
        route
        for route in routes
        if (isinstance(route, HttpRoute) and hosts & {*route.hosts})
        or (not isinstance(route, TcpRoute) and host in route.hosts)
    ]


def _hosts(config: Config, routes: Iterable[Route]) -> list[str]:
    zones = [domain.zone for domain in config.domains]
    errors = []
    owners: dict[str, list[Route]] = {}
    for route in routes:
        if isinstance(route, TcpRoute):
            continue
        for host in route.hosts:
            shared = (_shared_host(host, owner, route) for owner in owners.get(host, []))
            errors += [error for error in shared if error is not None]
            owners.setdefault(host, []).append(route)
            if not any(in_zone(host, zone) for zone in zones):
                errors.append(f"{host} is in no configured domain")
    return errors


def _shared_host(host: str, first: Route, second: Route) -> str | None:
    """Return why two routes cannot share a host, or None.

    Only `http` routes on different paths can, and only when they agree on the host's settings.
    """
    label = route_label(first)
    other = "another route" if label == host else label
    if not (isinstance(first, HttpRoute) and isinstance(second, HttpRoute)):
        return f"{host} is already served by {other}"
    if first.path_prefix == second.path_prefix:
        shown = host + first.path_prefix.rstrip("/")
        return f"{shown} is already served by {'another route' if label == shown else label}"
    differ = [
        word
        for name, word in HOST_SETTINGS.items()
        if getattr(first, name) != getattr(second, name)
    ]
    if differ:
        settings = " and ".join(differ)
        return f"{host} is shared with {other}, which has a different {settings}"
    return None


def _plain_http_hosts(config: Config, routes: Iterable[Route]) -> list[str]:
    """A route is served over HTTPS or over plain HTTP only, so its hosts cannot mix domains with
    and without a certificate."""
    errors = []
    for route in routes:
        if not isinstance(route, HttpRoute | RedirectRoute):
            continue
        domains = (domain_of(host, config.domains) for host in route.hosts)
        if len({domain.has_certificate for domain in domains if domain is not None}) > 1:
            errors.append(f"{route_label(route)}: hosts mix domains with and without a certificate")
    return errors


def _cloudflare_hosts(config: Config, routes: Iterable[Route]) -> list[str]:
    """Cloudflare DNS records exist only in Cloudflare zones. Elsewhere the switch would make a
    proxied route refuse every client outside the LAN, with no record to come through."""
    errors = []
    for route in routes:
        if isinstance(route, TcpRoute) or not route.cloudflare.dns:
            continue
        for host in route.hosts:
            domain = domain_of(host, config.domains)
            if domain is not None and domain.dns_provider != CLOUDFLARE:
                errors.append(
                    f"{host} is not in a Cloudflare domain; turn off its Cloudflare DNS switch"
                )
    return errors


def _zone_names(config: Config) -> list[str]:
    zones = [domain.zone for domain in config.domains]
    errors = [
        f"domain {domain.zone}: ddns_record must be inside the zone"
        for domain in config.domains
        if not in_zone(domain.ddns_record, domain.zone)
    ]
    errors += [
        f"dns record {record.name} is in no configured domain"
        for record in config.dns_records
        if not any(in_zone(record.name, zone) for zone in zones)
    ]
    return errors


def _port(listen: str) -> int:
    return int(listen.rpartition(":")[2])


def port_clashes(config: Config, routes: Iterable[Route]) -> list[str]:
    """Return the port clashes. HAProxy binds its ports on every address, so they clash with any
    other listener's port."""
    network = config.network
    haproxy = {network.http_port: "http_port", network.https_port: "https_port"}
    errors = []
    for route in routes:
        if not isinstance(route, TcpRoute):
            continue
        if route.listen_port in haproxy:
            used_by = haproxy[route.listen_port]
            errors.append(f"{route_label(route)} is already used by {used_by}")
        haproxy.setdefault(route.listen_port, "a tcp route")
    listeners = {"dashboard": config.dashboard.listen, "dns_server": config.dns_server.listen}
    if config.prometheus.enabled and config.prometheus.listen is not None:
        listeners["prometheus"] = config.prometheus.listen
    for name, listen in listeners.items():
        if _port(listen) in haproxy:
            errors.append(f"{name}.listen port is already used by {haproxy[_port(listen)]}")
    for (first, first_listen), (second, second_listen) in combinations(listeners.items(), 2):
        if (clash := _listener_clash(first_listen, second_listen)) is not None:
            errors.append(
                f"{second}.listen {second_listen} and the {first}'s address {first_listen} "
                f"share port {_port(first_listen)}{clash}; give one of them another port"
            )
    return errors


def _listener_clash(first: str, second: str) -> str | None:
    """Return why two listeners clash on one port, or None. They do when their IPs match, or when
    either one binds every address; the text then says so."""
    first_ip, _, first_port = first.rpartition(":")
    second_ip, _, second_port = second.rpartition(":")
    if first_port != second_port:
        return None
    if first_ip == second_ip:
        return ""
    if wildcard := {first_ip, second_ip} & {"0.0.0.0", "::"}:  # noqa: S104 - compared, not bound
        return f", and {wildcard.pop()} listens on every address"
    return None


def _resolve(host: str, port: int) -> set[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Return the addresses of a host as HAProxy's `init-addr libc` would find them."""
    try:
        answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError):
        # HAProxy disables a server whose name does not resolve, so it cannot reach anything.
        return set()
    return {ipaddress.ip_address(str(answer[4][0]).partition("%")[0]) for answer in answers}


def _dashboard_upstreams(config: Config, routes: Iterable[Route]) -> list[str]:
    """Refuse an upstream that reaches the dashboard: a public route would expose it.

    The dashboard runs on the HAProxy host, so a loopback or unspecified address, the dashboard's
    address and HAProxy's LAN address all reach it. Names are resolved, because `localhost:8443`
    passes the model and differs from the dashboard's address as text.
    """
    dashboard_ip, _, dashboard_port = config.dashboard.listen.rpartition(":")
    own = {ipaddress.ip_address(dashboard_ip), ipaddress.ip_address(config.network.haproxy_lan_ip)}
    errors = []
    for route in routes:
        if isinstance(route, RedirectRoute):
            continue
        for upstream in route.upstreams:
            host, _, port = upstream.address.rpartition(":")
            if port == dashboard_port and _reaches(_resolve(host, int(port)), own):
                errors.append(f"{route_label(route)}: upstream {upstream.address} is the dashboard")
    return errors


def fallback_warnings(config: Config, routes: Iterable[Route]) -> list[str]:
    """Warn about a public upstream on the dashboard's fallback address. The dashboard listens
    there, for private sources such as HAProxy, while `dashboard.listen` cannot be bound. Another
    service may own that port the rest of the time, so the route is not refused."""
    try:
        fallback_ip, fallback_port = bootstrap_listen(os.environ)
    except ServerConfigError:
        return []  # The dashboard cannot fall back at all, so it never listens there.
    if str(fallback_port) == config.dashboard.listen.rpartition(":")[2]:
        return []  # `_dashboard_upstreams` refuses it.
    own = {ipaddress.ip_address(fallback_ip), ipaddress.ip_address(config.network.haproxy_lan_ip)}
    warnings = []
    for route in routes:
        if isinstance(route, RedirectRoute) or route.exposure != "public":
            continue
        for upstream in route.upstreams:
            host, _, port = upstream.address.rpartition(":")
            if int(port) == fallback_port and _reaches(_resolve(host, int(port)), own):
                warnings.append(
                    f"{route_label(route)}: upstream {upstream.address} is where the dashboard "
                    "listens when dashboard.listen cannot be bound, and then this route would "
                    "make its login public"
                )
    return warnings


def _reaches(
    addresses: Iterable[ipaddress.IPv4Address | ipaddress.IPv6Address],
    own: set[ipaddress.IPv4Address | ipaddress.IPv6Address],
) -> bool:
    """Return whether an upstream reaches a listener of this host on one of `own`'s addresses."""
    # On 0.0.0.0 every address of this host reaches it, such as a second LAN or a VPN address.
    every_address = any(address.is_unspecified for address in own)
    return any(
        a in own or a.is_loopback or a.is_unspecified or (every_address and _on_this_host(a))
        for a in addresses
    )


def _on_this_host(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return whether an IPv4 address is one of this host's own, which only those can bind."""
    if address.version != 4:
        return False
    error = bind_error(str(address), 0)
    return error is None or error.errno != errno.EADDRNOTAVAIL


def _secrets(config: Config, load_secrets: Callable[[], Secrets]) -> list[str]:
    errors = []
    for domain in config.domains:
        if domain.credentials is None:
            continue  # A domain with no certificate has no credentials.
        credential = load_secrets().dns_credentials.get(domain.credentials)
        if credential is None:
            errors.append(
                f"domain {domain.zone}: credentials {domain.credentials!r} are not in secrets.yaml"
            )
        elif credential.provider != domain.dns_provider:
            errors.append(
                f"domain {domain.zone}: credentials {domain.credentials!r} are for "
                f"{credential.provider}, not {domain.dns_provider}"
            )
    if config.crowdsec.enabled:
        errors += crowdsec_key_errors(config.crowdsec, load_secrets().crowdsec)
    maxmind = config.geoip.provider == "maxmind"
    if maxmind and not load_secrets().geoip.maxmind_license_key.get_secret_value():
        errors.append("geoip: the MaxMind license key is not in secrets.yaml")
    return errors


def crowdsec_key_errors(crowdsec: Crowdsec, keys: SecretsCrowdsec) -> list[str]:
    """Return the keys that the bouncer needs with `crowdsec` and that are not set."""
    errors = []
    if not keys.bouncer_key.get_secret_value():
        errors.append("crowdsec: the bouncer key is not in secrets.yaml")
    needed = (keys.captcha_site_key, keys.captcha_secret_key, keys.captcha_signing_key)
    if crowdsec.captcha_provider != "none" and not all(k.get_secret_value() for k in needed):
        errors.append("crowdsec: the captcha keys are not in secrets.yaml")
    return errors
