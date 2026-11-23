"""Generated config running in a real HAProxy, because `haproxy -c` proves only that it parses.

The upstream is a test frontend inside the same HAProxy, so these tests need no other container.
Ports are published to random host ports, so parallel checkouts cannot clash. HAProxy sends its
access lines to a real `LogCollector` in this process, through a socket in a mounted directory.
"""

from pathlib import Path

import pytest

from homeproxy.util.run import run
from tests.golden.live_proxy import (
    HOME_CANARY,
    HTTP_PORT,
    HTTPS_PORT,
    LOCKED_PASSWORD,
    MISMATCHED_HOST,
    NOT_THE_CLIENT,
    OTHER_ADDRESS_UPSTREAM,
    OTHER_PORT_UPSTREAM,
    PASSTHROUGH_HOST,
    REQUEST_HEADERS_PATH,
    SPOOFABLE_HEADERS,
    UNDERSCORE_HEADERS,
    UPSTREAM,
    UPSTREAM_NAME,
    Proxy,
    Response,
    case_route,
    credentials,
    make_config,
    plain_route,
    reload_with_saved_state,
    running_proxy,
    upstream_reached,
    wait_for_log,
    write_test_release,
)


def test_public_route_serves_https_with_hsts(outside_proxy: Proxy) -> None:
    response = outside_proxy.get("app.example.com")
    assert response.status == 200
    assert response.headers["strict-transport-security"] == "max-age=31536000"


def test_route_hsts_header_wins_over_the_default(outside_proxy: Proxy) -> None:
    response = outside_proxy.get("own-hsts.example.com")
    assert response.headers["strict-transport-security"] == "max-age=300"


@pytest.mark.parametrize(
    "host",
    ["private.example.com", "PRIVATE.Example.COM", f"private.example.com:{HTTPS_PORT}"],
)
def test_lan_only_route_returns_403_outside_the_lan(outside_proxy: Proxy, host: str) -> None:
    assert outside_proxy.get(host).status == 403


def test_lan_only_route_serves_lan_clients_without_hsts(lan_proxy: Proxy) -> None:
    response = lan_proxy.get("private.example.com")
    assert response.status == 200
    assert "strict-transport-security" not in response.headers


def test_lan_check_follows_the_backend_whatever_host_leads_to_it(
    outside_proxy: Proxy, lan_proxy: Proxy
) -> None:
    assert outside_proxy.get(MISMATCHED_HOST).status == 403
    assert lan_proxy.get(MISMATCHED_HOST).status == 200


def test_second_host_header_cannot_bypass_the_lan_check(outside_proxy: Proxy) -> None:
    assert outside_proxy.get("app.example.com", "private.example.com").status == 400
    assert outside_proxy.get("private.example.com", "app.example.com").status == 400


@pytest.mark.parametrize("host", ["unknown.example.com", "private.example.com."])
def test_unknown_host_returns_404(outside_proxy: Proxy, host: str) -> None:
    response = outside_proxy.get(host)
    assert response.status == 404
    assert "strict-transport-security" not in response.headers


@pytest.mark.parametrize("host", ["global.example.com", "trailing.example.com"])
def test_route_with_reserved_name_serves(outside_proxy: Proxy, host: str) -> None:
    assert outside_proxy.get(host).status == 200


@pytest.mark.parametrize(
    "host",
    [
        "app.example.com",
        f"app.example.com:{HTTP_PORT}",
        "private.example.com",
        "unknown.example.com",
        PASSTHROUGH_HOST,
    ],
)
def test_http_redirects_to_https_with_path_and_port(outside_proxy: Proxy, host: str) -> None:
    """Plain HTTP never reaches a backend or a check, whatever the host: not the LAN-only rule, and
    not the 421 for a passthrough host, which shares the frontend with HTTPS."""
    response = outside_proxy.get_plain(host, path="/a/b?c=d")
    assert response.status == 301
    name = host.partition(":")[0]
    assert response.headers["location"] == f"https://{name}:{HTTPS_PORT}/a/b?c=d"
    assert "strict-transport-security" not in response.headers


def test_http_without_host_is_rejected(outside_proxy: Proxy) -> None:
    assert outside_proxy.get_plain().status == 400


def test_client_cannot_spoof_forwarded_headers(outside_proxy: Proxy) -> None:
    response = outside_proxy.get(
        "app.example.com",
        extra=(
            ("X-Forwarded-For", "198.51.100.1"),
            ("X-Forwarded-Proto", "http"),
            ("X-Forwarded-Host", "evil.example.net"),
            ("X-Forwarded-Port", "1"),
            ("X-Real-IP", "198.51.100.1"),
            ("Forwarded", "for=198.51.100.1;host=evil.example.net;proto=http"),
            *((name, "198.51.100.1") for name in UNDERSCORE_HEADERS),
        ),
    )
    _, count, forwarded_for, proto, *spoofable = response.body.split("|")
    assert count == "1"
    assert "198.51.100.1" not in forwarded_for
    assert proto == "https"
    assert dict(zip(SPOOFABLE_HEADERS, spoofable, strict=True)) == dict.fromkeys(
        SPOOFABLE_HEADERS, "0"
    )


@pytest.mark.parametrize(
    ("extra", "status"),
    [
        ((), 401),
        (credentials("alice", "wrong"), 401),
        (credentials("alice", "right"), 401),
        (credentials("bob", LOCKED_PASSWORD), 401),
        (credentials("alice", LOCKED_PASSWORD), 200),
    ],
    ids=["none", "wrong-password", "password-cut-at-colon", "unknown-user", "right"],
)
def test_basic_auth_route_serves_only_with_the_right_credentials(
    outside_proxy: Proxy, extra: tuple[tuple[str, str], ...], status: int
) -> None:
    response = outside_proxy.get("locked.example.com", path=REQUEST_HEADERS_PATH, extra=extra)
    assert response.status == status
    if status == 401:
        assert response.headers["www-authenticate"].startswith("Basic ")
    else:
        assert "authorization" not in upstream_headers(response)


def test_unverified_upstream_tls_connects(outside_proxy: Proxy) -> None:
    assert outside_proxy.get("tls-unverified.example.com").status == 200


def test_verified_upstream_tls_refuses_a_self_signed_certificate(outside_proxy: Proxy) -> None:
    assert outside_proxy.get("tls-verified.example.com").status == 503


def test_header_attack_values_reach_the_client_as_literal_text(outside_proxy: Proxy) -> None:
    expected = case_route("attack-header-values", "attack").response_headers
    response = outside_proxy.get("attack.example.com")
    assert response.status == 200
    assert {name: response.headers.get(name.lower()) for name in expected} == dict(expected)
    assert HOME_CANARY not in str(response.headers)


def upstream_headers(response: Response) -> dict[str, str]:
    """Return the request headers the test upstream echoed on `REQUEST_HEADERS_PATH`."""
    lines = (line.partition(": ") for line in response.body.splitlines() if line)
    return {name.lower(): value for name, _, value in lines}


def test_request_header_attack_values_reach_the_upstream_as_literal_text(
    outside_proxy: Proxy,
) -> None:
    expected = case_route("attack-header-values", "attack").request_headers
    response = outside_proxy.get("attack.example.com", path=REQUEST_HEADERS_PATH)
    assert response.status == 200
    got = upstream_headers(response)
    assert {name: got.get(name.lower()) for name in expected} == dict(expected)
    assert HOME_CANARY not in response.body


def test_quote_in_a_header_value_cannot_add_a_condition(outside_proxy: Proxy) -> None:
    """With an injected `if { always_false }`, the header would be missing from the response."""
    response = outside_proxy.get("attack.example.com")
    assert response.headers["x-quote-then-condition"] == 'x" if { always_false }'


@pytest.mark.parametrize("path", ["/%0a", "/%00"])
def test_health_check_sends_an_encoded_path_unchanged(outside_proxy: Proxy, path: str) -> None:
    wait_for_log(outside_proxy, rf'test_upstream test_upstream/<NOSRV> .*"GET {path} HTTP/1\.1"')


def test_reload_with_saved_state_follows_a_changed_upstream_port(
    data_dir: Path, haproxy_image: str
) -> None:
    with running_proxy(data_dir, NOT_THE_CLIENT, haproxy_image, (plain_route("app"),)) as proxy:
        assert upstream_reached(proxy, UPSTREAM) == UPSTREAM
        moved = plain_route("app", upstreams=[{"address": OTHER_PORT_UPSTREAM}])
        write_test_release(data_dir, "second", make_config(NOT_THE_CLIENT), (moved,))
        reload_with_saved_state(proxy)
        assert upstream_reached(proxy, OTHER_PORT_UPSTREAM) == OTHER_PORT_UPSTREAM


def test_reload_with_saved_state_follows_a_new_dns_answer(
    data_dir: Path, haproxy_image: str
) -> None:
    by_name = plain_route("app", upstreams=[{"address": f"{UPSTREAM_NAME}:9000"}])
    with running_proxy(data_dir, NOT_THE_CLIENT, haproxy_image, (by_name,)) as proxy:
        assert upstream_reached(proxy, UPSTREAM) == UPSTREAM
        new_answer = OTHER_ADDRESS_UPSTREAM.partition(":")[0]
        run(
            [
                "docker",
                "exec",
                "--user=0",
                proxy.container,
                "sh",
                "-c",
                f"echo '{new_answer} {UPSTREAM_NAME}' > /etc/hosts",
            ]
        )
        reload_with_saved_state(proxy)
        assert upstream_reached(proxy, OTHER_ADDRESS_UPSTREAM) == OTHER_ADDRESS_UPSTREAM
