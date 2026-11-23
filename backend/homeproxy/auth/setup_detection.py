"""Network values that setup writes without asking, detected from this host.

The LAN range is the connected route of the default route's interface, read from the kernel's
route table (`/proc/net/route`), so the netmask is the real one.

The HTTP and HTTPS ports are the only values detection can find taken: when another program, such
as the proxy HomeProxy replaces, already listens on 80 or 443, the first apply would fail, so setup
asks for that port.
"""

import ipaddress
import logging
import os
import socket
import struct
from pathlib import Path

from pydantic import BaseModel, Field

from homeproxy.auth.server import default_route_address
from homeproxy.haproxy.client import HAProxyClient, HAProxyError
from homeproxy.models.config import Network

logger = logging.getLogger(__name__)

ROUTE_TABLE = Path("/proc/net/route")
DNS_PORT = 5335
WEB_PORTS = {
    "network.http_port": Network.model_fields["http_port"].default,
    "network.https_port": Network.model_fields["https_port"].default,
}
PLACEHOLDER_ENV = "HOMEPROXY_PLACEHOLDER_HTTP_PORT"
# The same default as the HAProxy service's run script.
PLACEHOLDER_DEFAULT_PORT = "80"
PROBE_TIMEOUT = 1.0


class DetectedNetwork(BaseModel):
    """The `network` values setup writes."""

    lan_cidrs: list[str] | None = None
    haproxy_lan_ip: str | None = None


class DetectedDnsServer(BaseModel):
    """The `dns_server` values setup writes."""

    listen: str | None = None


class DetectedSetup(BaseModel):
    """The network values setup writes, in the shape of the setup request. A value that cannot
    be detected is None."""

    network: DetectedNetwork = Field(default_factory=DetectedNetwork)
    dns_server: DetectedDnsServer = Field(default_factory=DetectedDnsServer)

    def missing(self) -> list[str]:
        """Return the setup request fields that have no value, as dotted paths."""
        values: dict[str, object] = {
            "network.lan_cidrs": self.network.lan_cidrs,
            "network.haproxy_lan_ip": self.network.haproxy_lan_ip,
            "dns_server.listen": self.dns_server.listen,
        }
        return [path for path, value in values.items() if value is None]


def read_route_table() -> str:
    """Return the kernel's IPv4 route table, or an empty string if it cannot be read."""
    try:
        return ROUTE_TABLE.read_text(encoding="ascii")
    except OSError as error:
        logger.warning("cannot read %s: %s", ROUTE_TABLE, error)
        return ""


def _ipv4(field: str) -> ipaddress.IPv4Address:
    # The kernel writes each address as a hex number in host byte order.
    return ipaddress.IPv4Address(socket.inet_ntoa(struct.pack("=I", int(field, 16))))


def lan_network(address: str, route_table: str) -> str | None:
    """Return the network of the default route's interface that holds `address`, as a CIDR."""
    # Columns: Iface Destination Gateway Flags RefCnt Use Metric Mask ...
    rows = [line.split() for line in route_table.splitlines()[1:]]
    rows = [row for row in rows if len(row) >= 8]
    default_ifaces = {row[0] for row in rows if int(row[1], 16) == 0 and int(row[7], 16) == 0}
    host = ipaddress.IPv4Address(address)
    best: ipaddress.IPv4Network | None = None
    for row in rows:
        if row[0] not in default_ifaces or int(row[2], 16) != 0:
            continue
        network = ipaddress.IPv4Network(f"{_ipv4(row[1])}/{_ipv4(row[7])}")
        # A /32 or a catch-all range says nothing about the LAN.
        useful = 0 < network.prefixlen < 32 and host in network
        if useful and (best is None or network.prefixlen > best.prefixlen):
            best = network
    return None if best is None else str(best)


def detect_setup(address: str | None, route_table: str) -> DetectedSetup:
    """Return the values setup writes. `address` is the default route's IPv4 address."""
    network = lan_network(address, route_table) if address else None
    return DetectedSetup(
        network=DetectedNetwork(lan_cidrs=[network] if network else None, haproxy_lan_ip=address),
        dns_server=DetectedDnsServer(listen=f"{address}:{DNS_PORT}" if address else None),
    )


def detect_host_setup() -> DetectedSetup:
    """Return the values setup writes on this host, from its routes."""
    return detect_setup(default_route_address(), read_route_table())


def port_in_use(port: int, address: str | None) -> bool:
    """Return whether a program accepts connections on `port`, on loopback or on `address`.

    HAProxy binds the port on every address, so a program on either one blocks it. The API runs
    as non-root and cannot bind ports below 1024, so it connects instead.
    """
    for host in ("127.0.0.1", *([address] if address else [])):
        try:
            with socket.create_connection((host, port), timeout=PROBE_TIMEOUT):
                return True
        except OSError:
            continue
    return False


def placeholder_port() -> int | None:
    """Return the port HomeProxy's placeholder page holds, or None while HAProxy is not running.

    HAProxy stops at start when a bind fails, so a running placeholder holds its port.
    """
    try:
        HAProxyClient().show_info()
    except HAProxyError:
        return None
    return int(os.environ.get(PLACEHOLDER_ENV) or PLACEHOLDER_DEFAULT_PORT)


def taken_ports() -> list[str]:
    """Return the setup request fields of the default HTTP and HTTPS ports that another program
    on this host listens on."""
    address = default_route_address()
    ours = placeholder_port()
    return [path for path, port in WEB_PORTS.items() if port != ours and port_in_use(port, address)]
