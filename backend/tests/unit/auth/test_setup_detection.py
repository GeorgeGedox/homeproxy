"""Detection of the network values setup writes without asking."""

import socket
import struct

import pytest

from homeproxy.auth.setup_detection import (
    DetectedDnsServer,
    DetectedNetwork,
    DetectedSetup,
    detect_setup,
    port_in_use,
)

ZERO = "0.0.0.0"  # noqa: S104 - a route table value, nothing is bound
HEADER = "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT"


def hexip(address: str) -> str:
    """An address as the kernel writes it in `/proc/net/route`: hex in host byte order."""
    return f"{struct.unpack('=I', socket.inet_aton(address))[0]:08X}"


def route(iface: str, destination: str, gateway: str, mask: str) -> str:
    """One row of `/proc/net/route`."""
    return f"{iface}\t{hexip(destination)}\t{hexip(gateway)}\t0003\t0\t0\t0\t{hexip(mask)}\t0\t0\t0"


def table(*rows: str) -> str:
    """A route table with a header."""
    return "\n".join([HEADER, *rows]) + "\n"


DEFAULT = route("eth0", ZERO, "192.168.1.1", ZERO)
LAN_24 = route("eth0", "192.168.1.0", ZERO, "255.255.255.0")
LAN_16 = route("eth0", "192.168.0.0", ZERO, "255.255.0.0")


@pytest.mark.parametrize(
    ("routes", "expected"),
    [
        pytest.param(table(DEFAULT, LAN_24), ["192.168.1.0/24"], id="home-lan"),
        pytest.param(table(DEFAULT, LAN_16), ["192.168.0.0/16"], id="netmask-is-read-not-guessed"),
        pytest.param(
            table(DEFAULT, LAN_16, LAN_24),
            ["192.168.1.0/24"],
            id="narrowest-connected-route",
        ),
        pytest.param(
            table(DEFAULT, route("docker0", "192.168.1.0", ZERO, "255.255.255.0")),
            None,
            id="other-interface",
        ),
        pytest.param(
            table(DEFAULT, route("eth0", "192.168.1.10", ZERO, "255.255.255.255")),
            None,
            id="host-route-only",
        ),
        pytest.param(table(LAN_24), None, id="no-default-route"),
        pytest.param("", None, id="unreadable-table"),
    ],
)
def test_lan_range_is_the_default_interface_network(
    routes: str, expected: list[str] | None
) -> None:
    """The LAN range is the connected network of the default route's interface, or missing."""
    detected = detect_setup("192.168.1.10", routes)
    assert detected == DetectedSetup(
        network=DetectedNetwork(lan_cidrs=expected, haproxy_lan_ip="192.168.1.10"),
        dns_server=DetectedDnsServer(listen="192.168.1.10:5335"),
    )
    assert detected.missing() == ([] if expected else ["network.lan_cidrs"])


def test_without_a_default_route_every_network_value_is_missing() -> None:
    """With no address to detect, the request must carry every network value."""
    detected = detect_setup(None, table(LAN_24))
    assert detected == DetectedSetup()
    assert detected.missing() == [
        "network.lan_cidrs",
        "network.haproxy_lan_ip",
        "dns_server.listen",
    ]


def test_a_port_is_in_use_while_a_program_listens_on_it() -> None:
    """The probe connects, so it finds a listener without binding the port itself."""
    with socket.create_server(("127.0.0.1", 0)) as listener:
        port = listener.getsockname()[1]
        assert port_in_use(port, None)
    assert not port_in_use(port, None)
