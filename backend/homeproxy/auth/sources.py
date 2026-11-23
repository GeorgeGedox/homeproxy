"""Which client addresses may talk to the dashboard.

In setup mode only private ranges may connect, because the LAN ranges are not known yet. After
setup only `network.lan_cidrs` may connect. The server drops any other connection before it reads
a request, so such a client gets no HTTP response.
"""

import ipaddress
from collections.abc import Iterable
from dataclasses import dataclass

type IpNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
type IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

PRIVATE_RANGES: tuple[IpNetwork, ...] = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
    )
)
"""RFC 1918, loopback, link-local and IPv6 unique local ranges. Carrier-grade NAT space
(100.64.0.0/10) is left out, because it is shared with other customers of the same ISP."""


def parse_address(text: str | None) -> IpAddress | None:
    """Parse a peer address, unwrapping IPv4-mapped IPv6. Return None for anything else."""
    if not text:
        return None
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


@dataclass(frozen=True)
class SourcePolicy:
    """A set of networks that may connect."""

    networks: tuple[IpNetwork, ...]
    label: str

    def allows(self, peer: str | None) -> bool:
        """Return whether a peer address lies in one of the networks."""
        address = parse_address(peer)
        if address is None:
            return False
        return any(
            address.version == network.version and address in network for network in self.networks
        )


def private_policy() -> SourcePolicy:
    """The policy for setup mode: private ranges only."""
    return SourcePolicy(PRIVATE_RANGES, "private ranges")


def lan_policy(cidrs: Iterable[str]) -> SourcePolicy:
    """The policy after setup: the configured LAN ranges only."""
    networks = tuple(ipaddress.ip_network(cidr, strict=True) for cidr in cidrs)
    return SourcePolicy(networks, "network.lan_cidrs")
