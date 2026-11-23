"""CrowdSec decisions enforced by the bouncer and HAProxy in the built image, against a real LAPI.

The image runs as in production, under s6, on a data volume seeded with a config that enables
CrowdSec. The test applies it with the real pipeline inside the container, which writes the
bouncer's config. A CrowdSec engine in its own container holds the decisions. Each test source is
banned there.

Clients need different source addresses. Relays in the same HAProxy, from an include, forward to
the HTTPS frontend from fixed loopback addresses: Cloudflare's edge, the LAN, the allowlist and an
outside client. TCP relays reach a `tcp` route and a `passthrough` route from the same addresses.
Each apply makes the test edge the only Cloudflare range at runtime, because a
release holds only public ranges. The test reaches the relays on the container's own address, so
no host port is bound.
"""

import socket
import subprocess
import threading
import time

import pytest

from tests.conftest import wait_until
from tests.integration.crowdsec_lab import (
    ALLOWED,
    BANNED_VISITOR,
    BOUNCER_CONFIG,
    BOUNCER_KEY,
    CLEAN_VISITOR,
    CLOUDFLARE,
    GAME_PORT,
    LAN,
    OUTSIDE,
    RELAY_PORTS,
    Lab,
    logs,
)
from tests.integration.haproxy_live import docker

SPOA_SERVICE = "/run/service/spoa"


@pytest.mark.parametrize(
    ("source", "cf_value", "status"),
    [
        pytest.param(OUTSIDE, None, 403, id="banned-client"),
        pytest.param(CLOUDFLARE, BANNED_VISITOR, 403, id="banned-visitor-through-cloudflare"),
        pytest.param(CLOUDFLARE, CLEAN_VISITOR, 200, id="banned-edge-is-never-checked"),
        pytest.param(LAN, None, 200, id="banned-lan-is-never-checked"),
        pytest.param(CLOUDFLARE, LAN, 403, id="lan-named-through-cloudflare-is-checked"),
        pytest.param(ALLOWED, None, 200, id="banned-allowlist-is-never-checked"),
    ],
)
def test_decisions_apply_to_the_real_client(
    lab: Lab, source: str, cf_value: str | None, status: int
) -> None:
    response = lab.get(source, cf_value)

    assert response.status_code == status
    if status == 403:
        assert "<title>CrowdSec Ban</title>" in response.text
    else:
        assert response.text == (cf_value or source)


@pytest.mark.parametrize(
    ("source", "target", "served"),
    [
        pytest.param(OUTSIDE, GAME_PORT, False, id="tcp-banned-client"),
        pytest.param(LAN, GAME_PORT, True, id="tcp-banned-lan-is-never-checked"),
        # A tcp route has no Cloudflare option, so a Cloudflare address there is a plain client.
        pytest.param(CLOUDFLARE, GAME_PORT, False, id="tcp-banned-cloudflare-address-is-checked"),
        pytest.param(OUTSIDE, 8443, False, id="passthrough-banned-client"),
        pytest.param(ALLOWED, 8443, True, id="passthrough-banned-allowlist-is-never-checked"),
    ],
)
def test_bans_reach_tcp_and_passthrough_routes(
    lab: Lab, source: str, target: int, served: bool
) -> None:
    assert lab.stream_served(source, target) is served


def test_late_hello_still_leaves_the_bouncer_time_to_answer(lab: Lab) -> None:
    """The SNI layer waits up to 5 s for the hello. A check that shared that wait would fail open
    when a hello came late and the bouncer took a moment to answer."""
    bouncer = '"$(cat /run/crowdsec-spoa/spoa.pid)"'
    hello_at, answer_after = 4.85, 0.1

    def signal(name: str) -> None:
        docker("exec", lab.homeproxy, "sh", "-c", f"kill -{name} {bouncer}")

    # The bouncer is paused before the hello and answers about 0.1 s plus one `docker exec` after
    # it: past the end of the SNI layer's wait, and within the SPOE processing timeout of 0.5 s.
    timers = [
        threading.Timer(2.0, signal, ["STOP"]),
        threading.Timer(hello_at + answer_after, signal, ["CONT"]),
    ]
    for timer in timers:
        timer.start()
    try:
        assert not lab.stream_served(OUTSIDE, 8443, hello_delay=hello_at)
    finally:
        for timer in timers:
            timer.join()


def test_slow_body_from_the_lan_is_not_cut_off(lab: Lab) -> None:
    """Only checked requests wait for a body, so a slow upload from the LAN keeps going."""
    head = b"POST / HTTP/1.1\r\nHost: app.example.com\r\nTransfer-Encoding: chunked\r\n\r\n"
    with socket.create_connection((lab.address, RELAY_PORTS[LAN]), timeout=30) as client:
        client.sendall(head + b"5\r\nhello\r\n")
        # Longer than `timeout http-request`, within which a buffered body must arrive.
        time.sleep(12)
        client.sendall(b"0\r\n\r\n")
        reply = client.recv(4096)
    assert reply.startswith(b"HTTP/1.1 200 "), reply


def test_traffic_flows_while_the_bouncer_is_stopped(lab: Lab) -> None:
    """Also: the status shows the bouncer down while it is stopped, and up again after."""
    wait_until(
        lambda: lab.bouncer() == "up",
        "the status to show the bouncer up",
        timeout=60,
        interval=0.5,
        not_yet=(Exception,),
    )
    docker("exec", lab.homeproxy, "/command/s6-svc", "-d", SPOA_SERVICE)
    try:
        lab.wait_for_bouncer_down()
        wait_until(
            lambda: lab.bouncer() == "down",
            "the status to show the bouncer down",
            timeout=30,
            interval=0.5,
            not_yet=(Exception,),
        )
        started = time.monotonic()
        assert lab.get(OUTSIDE).status_code == 200
        # Fail-open must not cost a timeout per request.
        assert time.monotonic() - started < 1.0
        started = time.monotonic()
        assert lab.stream_served(OUTSIDE, GAME_PORT)
        assert time.monotonic() - started < 1.0
    finally:
        docker("exec", lab.homeproxy, "/command/s6-svc", "-u", SPOA_SERVICE)
    lab.wait_for_ban()
    wait_until(
        lambda: lab.bouncer() == "up",
        "the status to show the bouncer up",
        timeout=30,
        interval=0.5,
        not_yet=(Exception,),
    )


def test_disabling_crowdsec_removes_the_bouncer_config_and_stops_it(lab: Lab) -> None:
    lab.apply(enabled=False)
    try:
        assert not lab.exists(BOUNCER_CONFIG)
        assert lab.bouncer() == "None"
        lab.wait_for_bouncer_down()
        assert lab.get(OUTSIDE).status_code == 200
    finally:
        lab.apply(enabled=True)
    lab.wait_for_ban()


def test_the_bouncer_key_never_reaches_the_container_log_or_the_stored_log(lab: Lab) -> None:
    assert BOUNCER_KEY not in logs(lab.homeproxy)
    read = ["docker", "exec", lab.homeproxy, "cat", "/data/state/logs/spoa/current"]
    stored = subprocess.run(read, capture_output=True, text=True, check=True).stdout
    assert "Starting crowdsec-spoa-bouncer" in stored
    assert BOUNCER_KEY not in stored
