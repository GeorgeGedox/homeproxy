"""Parsers on text captured from a real haproxy:3.4 and on broken input."""

import pytest

from homeproxy.haproxy.stats import ProxyType, StatsParseError, parse_info, parse_stat

# Captured from haproxy:3.4.7, shortened to fewer columns. The column names, the trailing
# comma and the final empty line are as HAProxy writes them.
STAT_HEADER = (
    "# pxname,svname,scur,stot,bin,bout,ereq,econ,eresp,status,lastchg,type,check_status,"
    "hrsp_2xx,hrsp_4xx,hrsp_5xx,req_tot,last_chk,\n"
)
STAT = (
    STAT_HEADER
    + "web,FRONTEND,1,7,100,200,3,,,OPEN,,0,,5,1,0,6,,\n"
    + 'b1,s1,0,2,10,20,,4,5,DOWN,12,2,L4CON,1,0,1,,"refused, by OS",\n'
    + "b1,BACKEND,0,2,10,20,0,4,5,DOWN,12,1,,1,0,1,2,,\n\n"
)
INFO = (
    "Name: HAProxy\nVersion: 3.4.7-8872a26\nRelease_date: 2026/10/14\nPid: 25\n"
    "Uptime: 0d 0h00m40s\nUptime_sec: 40\nnode: \n\n"
)


def test_parse_info_reads_the_named_fields() -> None:
    info = parse_info(INFO)
    assert info.version == "3.4.7-8872a26"
    assert info.pid == 25
    assert info.uptime_seconds == 40


@pytest.mark.parametrize(
    "text",
    [
        "",
        "\n",
        "Permission denied\n\n",
        "Name: HAProxy\nPid: 25\nUptime_sec: 40\n\n",
        "Version: 3.4\nUptime_sec: 40\n\n",
        "Version: 3.4\nPid: 25\n\n",
        "Version: 3.4\nPid: many\nUptime_sec: 40\n\n",
        "Version: \nPid: 25\nUptime_sec: 40\n\n",
    ],
)
def test_parse_info_rejects_text_without_required_fields(text: str) -> None:
    with pytest.raises(StatsParseError):
        parse_info(text)


def test_parse_stat_reads_every_row_type() -> None:
    frontend, server, backend = parse_stat(STAT)

    assert (frontend.proxy, frontend.service, frontend.type) == (
        "web",
        "FRONTEND",
        ProxyType.FRONTEND,
    )
    assert frontend.status == "OPEN"
    assert (frontend.responses_5xx, frontend.requests_total) == (0, 6)

    # The server row's last column holds a quoted comma.
    assert (server.service, server.type, server.status) == ("s1", ProxyType.SERVER, "DOWN")
    assert server.check_status == "L4CON"

    assert backend.type is ProxyType.BACKEND


def test_parse_stat_gives_none_for_empty_counters() -> None:
    frontend, server, _ = parse_stat(STAT)
    assert frontend.check_status == ""
    assert server.requests_total is None


def test_parse_stat_finds_columns_by_name() -> None:
    text = "# type,new_column,svname,pxname,\n2,x,s1,b1,\n\n"
    row = parse_stat(text)[0]
    assert (row.proxy, row.service, row.type) == ("b1", "s1", ProxyType.SERVER)
    assert row.requests_total is None


def test_parse_stat_accepts_a_header_with_no_rows() -> None:
    assert parse_stat(STAT_HEADER + "\n") == []


@pytest.mark.parametrize(
    "text",
    [
        "",
        "\n",
        "Permission denied\n\n",
        "pxname,svname,type,\nweb,FRONTEND,0,\n\n",
        "# pxname,svname,\nweb,FRONTEND,\n\n",
        "# pxname,svname,type,\nweb,FRONTEND,\n\n",
        "# pxname,svname,type,\nweb,FRONTEND,0,1,\n\n",
        "# pxname,svname,type,\nweb,FRONTEND,9,\n\n",
        "# pxname,svname,type,\nweb,FRONTEND,x,\n\n",
        "# pxname,svname,type,\nweb,FRONTEND,,\n\n",
        "# pxname,svname,type,req_tot,\nweb,FRONTEND,0,many,\n\n",
    ],
)
def test_parse_stat_rejects_text_that_is_not_stat_output(text: str) -> None:
    with pytest.raises(StatsParseError):
        parse_stat(text)
