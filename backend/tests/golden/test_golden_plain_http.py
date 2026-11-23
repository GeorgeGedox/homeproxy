"""Routes on a domain with no certificate, in a real HAProxy: plain HTTP only, with every check.

`example.net` has no DNS provider. `example.com` has a certificate for itself and `*.example.com`
in the certs directory, so the tests can show that an `example.net` host never gets it.
`example.org` is self-signed, with its own bundle. The test client is outside the LAN, and the
test country map puts every address in FR.
"""

import socket
import ssl
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.x509 import load_pem_x509_certificate

from homeproxy.certs.selfsigned import self_signed_bundle
from homeproxy.models import parse_route
from tests.golden.live_proxy import (
    HTTPS_PORT,
    LOCKED_PASSWORD,
    NOT_THE_CLIENT,
    OTHER_PORT_UPSTREAM,
    UPSTREAM,
    Proxy,
    credentials,
    plain_route,
    running_proxy,
    tls_request,
)

DOMAINS = {
    "domains": [
        {"zone": "example.com", "dns_provider": "cloudflare", "credentials": "cf-main"},
        {"zone": "example.net"},
        {"zone": "example.org", "dns_provider": "self-signed"},
    ],
}
ROUTES = (
    plain_route("app"),
    plain_route("signed", hosts=["app.example.org"]),
    plain_route("plain", hosts=["plain.example.net"]),
    plain_route("private", exposure="lan", hosts=["private.example.net"]),
    plain_route("locked", auth={"type": "basic"}, hosts=["locked.example.net"]),
    plain_route("blocked", geoip={"block_countries": ["FR"]}, hosts=["blocked.example.net"]),
    plain_route("site", hosts=["paths.example.net"]),
    plain_route(
        "api",
        hosts=["paths.example.net"],
        upstreams=[{"address": OTHER_PORT_UPSTREAM}],
        path_prefix="/api",
    ),
    parse_route(
        {
            "version": 1,
            "name": "www",
            "type": "redirect",
            "hosts": ["www.example.net"],
            "exposure": "public",
            "redirect": {"to": "http://plain.example.net/", "keep_path": True},
        }
    ),
)


def first_certificate(path: Path) -> bytes:
    """Return the first certificate of a PEM bundle, as a TLS handshake shows it."""
    return load_pem_x509_certificate(path.read_bytes()).public_bytes(Encoding.DER)


@pytest.fixture(scope="module")
def data_dir(make_data_dir: Callable[[], Path]) -> Path:
    data_dir = make_data_dir()
    bundle = self_signed_bundle(("example.com", "*.example.com"))
    (data_dir / "certs" / "example.com.pem").write_bytes(bundle)
    signed = self_signed_bundle(("example.org", "*.example.org"))
    (data_dir / "certs" / "example.org.pem").write_bytes(signed)
    return data_dir


@pytest.fixture(scope="module")
def proxy(data_dir: Path, haproxy_image: str) -> Iterator[Proxy]:
    with running_proxy(
        data_dir, NOT_THE_CLIENT, haproxy_image, routes=ROUTES, config_changes=DOMAINS
    ) as running:
        running.runtime("add map country.map 0.0.0.0/0 FR")
        yield running


def served_certificate(proxy: Proxy, sni: str) -> bytes:
    """Return the certificate that the HTTPS port shows for this SNI name."""
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with (
        socket.create_connection(("127.0.0.1", proxy.https_port), timeout=10) as raw,
        context.wrap_socket(raw, server_hostname=sni) as tls,
    ):
        return tls.getpeercert(binary_form=True) or b""


def test_route_answers_over_plain_http_without_redirect_or_hsts(proxy: Proxy) -> None:
    response = proxy.get_plain("plain.example.net")
    assert response.status == 200
    assert "location" not in response.headers
    assert "strict-transport-security" not in response.headers
    # The upstream is told the truth, so the links it builds work.
    assert response.body.split("|")[3] == "http"


def test_redirect_route_answers_over_plain_http(proxy: Proxy) -> None:
    response = proxy.get_plain("www.example.net", path="/a?b=1")
    assert (response.status, response.headers["location"]) == (
        301,
        "http://plain.example.net/a?b=1",
    )


@pytest.mark.parametrize(
    ("host", "extra", "status"),
    [
        pytest.param("private.example.net", (), 403, id="lan-only"),
        pytest.param("locked.example.net", (), 401, id="basic-auth"),
        pytest.param(
            "locked.example.net", credentials("alice", LOCKED_PASSWORD), 200, id="basic-auth-ok"
        ),
        pytest.param("blocked.example.net", (), 403, id="geoip"),
    ],
)
def test_every_check_applies_over_plain_http(
    proxy: Proxy, host: str, extra: tuple[tuple[str, str], ...], status: int
) -> None:
    assert proxy.get_plain(host, extra=extra).status == status


def test_paths_reach_their_own_routes_over_plain_http(proxy: Proxy) -> None:
    assert proxy.get_plain("paths.example.net", path="/api/x").body.split("|")[0] == (
        OTHER_PORT_UPSTREAM
    )
    assert proxy.get_plain("paths.example.net", path="/apiv2").body.split("|")[0] == UPSTREAM


@pytest.mark.parametrize("host", ["plain.example.net", "www.example.net"])
def test_https_port_treats_the_host_as_unknown_with_the_default_certificate(
    proxy: Proxy, data_dir: Path, host: str
) -> None:
    default = first_certificate(data_dir / "certs" / "default.pem")
    example_com = first_certificate(data_dir / "certs" / "example.com.pem")
    port = proxy.https_port

    assert served_certificate(proxy, "app.example.com") == example_com
    assert tls_request(port, "app.example.com", "app.example.com", "/").startswith("HTTP/1.1 200")
    assert served_certificate(proxy, host) == default
    assert tls_request(port, host, host, "/").startswith("HTTP/1.1 404")


def test_self_signed_domain_answers_over_https_and_redirects_from_http(
    proxy: Proxy, data_dir: Path
) -> None:
    port = proxy.https_port
    location = proxy.get_plain("app.example.org").headers["location"]

    assert location == f"https://app.example.org:{HTTPS_PORT}/"
    assert served_certificate(proxy, "app.example.org") == first_certificate(
        data_dir / "certs" / "example.org.pem"
    )
    assert tls_request(port, "app.example.org", "app.example.org", "/").startswith("HTTP/1.1 200")
