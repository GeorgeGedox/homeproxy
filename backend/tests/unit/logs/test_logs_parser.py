"""The parser reads both access line formats. The sample lines are copied from HAProxy 3.4."""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from homeproxy.logs import AccessLog, LogCollector, parse_message

HEADER = "<134>1 2026-11-09T17:08:10.587251+00:00 - haproxy 8 - - "
HTTPS_LINE = (
    "172.17.0.1:60834 [09/Nov/2026:17:08:10.586] fe_https~ route-app/192.168.1.20:8080 "
    '0/0/1/2/3 200 512 - - ---- 2/1/0/0/0 0/0 "GET /a?b=c HTTP/1.1" "app" "DE" "app.example.com"'
)
TCP_LINE = (
    "172.17.0.1:60858 [09/Nov/2026:17:08:11.058] fe_sni be_pass/s 1/0/2 2057 -- "
    '2/1/0/0/0 0/0 "pass" "" "pass.example.com"'
)


def datagram(message: str, header: str = HEADER) -> bytes:
    return f"{header}{message}\n".encode()


def test_https_line() -> None:
    entry = LogCollector().receive(datagram(HTTPS_LINE))
    assert entry == AccessLog(
        kind="http",
        logged_at=datetime(2026, 11, 9, 17, 8, 10, 587251, tzinfo=UTC),
        accepted_at=datetime(2026, 11, 9, 17, 8, 10, 586000, tzinfo=UTC),
        client_ip="172.17.0.1",
        client_port=60834,
        frontend="fe_https",
        tls=True,
        backend="route-app",
        server="192.168.1.20:8080",
        total_time_ms=3,
        status=200,
        bytes_sent=512,
        termination_state="----",
        method="GET",
        request="GET /a?b=c HTTP/1.1",
        route="app",
        country="DE",
        sni="app.example.com",
        line=HTTPS_LINE,
    )


def test_tcp_line() -> None:
    entry = LogCollector().receive(datagram(TCP_LINE))
    assert entry is not None
    assert entry.kind == "tcp"
    assert (entry.frontend, entry.tls) == ("fe_sni", False)
    assert (entry.backend, entry.server) == ("be_pass", "s")
    assert (entry.total_time_ms, entry.bytes_sent, entry.termination_state) == (2, 2057, "--")
    assert (entry.status, entry.method, entry.request) == (None, None, None)
    assert (entry.route, entry.country, entry.sni) == ("pass", None, "pass.example.com")


def test_line_for_an_unknown_host_has_no_route() -> None:
    line = (
        "203.0.113.5:4000 [09/Nov/2026:17:00:00.000] fe_https~ be_not_found/<NOSRV> "
        '0/-1/-1/-1/0 404 0 - - LR-- 1/1/0/0/0 0/0 "GET / HTTP/2.0" "" "" ""'
    )
    entry = LogCollector().receive(datagram(line))
    assert entry is not None
    assert (entry.backend, entry.server, entry.status) == ("be_not_found", "<NOSRV>", 404)
    assert (entry.route, entry.country, entry.sni) == (None, None, None)


def test_ipv6_client() -> None:
    line = HTTPS_LINE.replace("172.17.0.1:60834", "2001:db8::1:443")
    entry = LogCollector().receive(datagram(line))
    assert entry is not None
    assert (entry.client_ip, entry.client_port) == ("2001:db8::1", 443)


def test_unknown_times_and_status_are_none() -> None:
    line = (
        "192.168.1.5:5000 [09/Nov/2026:17:00:00.000] fe_https~ fe_https/<NOSRV> "
        '-1/-1/-1/-1/+5 -1 +0 - - CR-- 1/1/0/0/0 0/0 "<BADREQ>" "" "" ""'
    )
    entry = LogCollector().receive(datagram(line))
    assert entry is not None
    assert (entry.total_time_ms, entry.status, entry.bytes_sent) == (5, None, 0)
    assert (entry.request, entry.method) == ("<BADREQ>", None)


def test_header_captures_are_skipped() -> None:
    line = HTTPS_LINE.replace(' 0/0 "GET', ' 0/0 {agent|x} {text/html} "GET')
    entry = LogCollector().receive(datagram(line))
    assert entry is not None
    assert entry.request == "GET /a?b=c HTTP/1.1"


def test_encoded_sni_is_kept_encoded() -> None:
    line = HTTPS_LINE.replace('"app.example.com"', '"a%0Ab%22c%20d"')
    entry = LogCollector().receive(datagram(line))
    assert entry is not None
    assert entry.sni == "a%0Ab%22c%20d"


def test_accept_time_takes_the_offset_of_the_syslog_timestamp() -> None:
    header = HEADER.replace("+00:00", "+02:00")
    entry = LogCollector().receive(datagram(HTTPS_LINE, header))
    assert entry is not None
    offset = timezone(timedelta(hours=2))
    assert entry.accepted_at == datetime(2026, 11, 9, 17, 8, 10, 586000, tzinfo=offset)
    assert entry.logged_at.utcoffset() == timedelta(hours=2)


def test_parse_message_without_a_syslog_header() -> None:
    logged_at = datetime(2026, 11, 9, tzinfo=UTC)
    entry = parse_message(TCP_LINE, logged_at)
    assert entry is not None
    assert entry.logged_at == logged_at


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"\xff\xfe\x00garbage",
        datagram("Server route-app/192.168.1.20:8080 is DOWN, reason: Layer4 timeout."),
        datagram("Proxy fe_https started."),
        # The appended fields are missing, as from a config that HomeProxy did not write.
        datagram(HTTPS_LINE.rsplit(' "app"', 1)[0]),
        datagram(HTTPS_LINE.replace("09/Nov/2026", "31/Feb/2026")),
        datagram(HTTPS_LINE.replace("09/Nov/2026", "09/Foo/2026")),
        datagram(HTTPS_LINE, HEADER.replace("<134>", "<999>")),
        datagram(HTTPS_LINE, HEADER.replace("+00:00", "")),
        datagram(HTTPS_LINE, HEADER.replace("2026-11-09T", "yesterday-")),
        datagram(HTTPS_LINE, ""),
        datagram(HTTPS_LINE + " trailing"),
    ],
    ids=[
        "empty",
        "binary",
        "server-down",
        "proxy-started",
        "no-appended-fields",
        "impossible-date",
        "unknown-month",
        "bad-priority",
        "timestamp-without-offset",
        "bad-timestamp",
        "no-syslog-header",
        "extra-field",
    ],
)
def test_other_input_is_not_an_access_line(data: bytes) -> None:
    assert LogCollector().receive(data) is None
