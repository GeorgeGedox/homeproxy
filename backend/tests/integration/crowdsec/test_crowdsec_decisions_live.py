"""Decisions through the LAPI client, and the HAProxy-side allowlist, against the bouncer lab.

The lab is the one in `conftest.py`: the built image under s6 with CrowdSec on, a real
CrowdSec engine, and relays that give each client its own source address. The LAPI client runs in
the test process, as the dashboard's machine. Allowlist edits run in the container, as the API
user, with the real store and HAProxy sockets.
"""

import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from homeproxy.crowdsec import LapiClient
from homeproxy.crowdsec.health import connection_check
from homeproxy.models.config import Crowdsec
from homeproxy.models.secrets import SecretsCrowdsec
from homeproxy.store import load_tree
from tests.conftest import wait_until
from tests.integration.crowdsec_lab import (
    ALLOWED,
    APPLY,
    BANNED_VISITOR,
    BOUNCER_KEY,
    CLEAN_VISITOR,
    CLOUDFLARE,
    DECISION_TIMEOUT,
    OUTSIDE,
    SEED,
    Lab,
)
from tests.integration.haproxy_live import docker

MACHINE = load_tree(SEED / "secrets" / "secrets.yaml")["crowdsec"]
PYTHON = "/opt/homeproxy/venv/bin/python"
UNBAN_SECONDS = 15.0
"""The bouncer pulls decisions every 10 s."""

ALLOWLIST = """
import sys
from pathlib import Path
from homeproxy.crowdsec import add_to_allowlist, remove_from_allowlist
from homeproxy.haproxy.client import HAProxyClient
from homeproxy.store import Store
edit = add_to_allowlist if sys.argv[1] == "add" else remove_from_allowlist
for entry in sys.argv[2:]:
    edit(Store(Path("/data")), HAProxyClient(), entry)
"""


@pytest.fixture(scope="session")
def lapi(lab: Lab) -> LapiClient:
    """The dashboard's machine, registered in the lab's engine with the seed's credentials."""
    machine_id, password = MACHINE["machine_id"], MACHINE["machine_password"]
    add = ["cscli", "machines", "add", machine_id, "--password", password, "-f", "-"]
    docker("exec", lab.crowdsec, *add)
    return LapiClient(f"http://{lab.lapi}:8080", machine_id, password)


def allowlist(lab: Lab, action: str, *entries: str) -> None:
    docker("exec", "-u", "homeproxy", lab.homeproxy, PYTHON, "-c", ALLOWLIST, action, *entries)


def status(lab: Lab, source: str, cf_value: str | None = None) -> int:
    return lab.get(source, cf_value).status_code


def test_unban_restores_access_within_seconds(lab: Lab, lapi: LapiClient) -> None:
    ban = next(d for d in lapi.decisions() if d.value == OUTSIDE)
    assert (ban.type, ban.scope) == ("ban", "Ip")
    try:
        lapi.delete_decision(ban.id)
        started = time.monotonic()
        wait_until(
            lambda: status(lab, OUTSIDE) == 200,
            "the unban",
            timeout=DECISION_TIMEOUT,
            interval=0.5,
            not_yet=(Exception,),
        )
        assert time.monotonic() - started < UNBAN_SECONDS
        assert OUTSIDE not in [d.value for d in lapi.decisions()]
    finally:
        docker("exec", lab.crowdsec, "cscli", "decisions", "add", "--ip", OUTSIDE)
    lab.wait_for_ban()


def test_a_manual_ban_is_listed_and_enforced(lab: Lab, lapi: LapiClient) -> None:
    def visitor(code: int) -> bool:
        return status(lab, CLOUDFLARE, CLEAN_VISITOR) == code

    lapi.add_decision(CLEAN_VISITOR, "ban", timedelta(hours=1))
    try:
        [ban] = [d for d in lapi.decisions() if d.value == CLEAN_VISITOR]
        assert (ban.type, ban.scope, ban.origin) == ("ban", "Ip", "cscli")
        left = ban.expires_at - datetime.now(UTC)
        assert timedelta(minutes=59) < left <= timedelta(hours=1)
        wait_until(
            lambda: visitor(403),
            "the manual ban",
            timeout=DECISION_TIMEOUT,
            interval=0.5,
            not_yet=(Exception,),
        )
    finally:
        for decision in lapi.decisions():
            if decision.value == CLEAN_VISITOR:
                lapi.delete_decision(decision.id)
    wait_until(
        lambda: visitor(200),
        "the unban",
        timeout=DECISION_TIMEOUT,
        interval=0.5,
        not_yet=(Exception,),
    )


@pytest.fixture
def restore_config(lab: Lab) -> Iterator[None]:
    """Put back the lab's config, allowlist and runtime Cloudflare range after the test."""
    yield
    lab.apply(enabled=True)
    lab.wait_for_ban()


@pytest.mark.usefixtures("restore_config")
def test_allowlist_takes_effect_at_once_and_survives_a_restart(lab: Lab) -> None:
    def enforcing() -> bool:
        return status(lab, CLOUDFLARE, BANNED_VISITOR) == 403

    allowlist(lab, "add", OUTSIDE)
    assert status(lab, OUTSIDE) == 200

    # A restart drops every runtime change, so the entry must be in the live release.
    restart = ["/command/s6-svc", "-T", "30000", "-wr", "-r", "/run/service/haproxy"]
    docker("exec", lab.homeproxy, *restart)
    wait_until(
        enforcing,
        "the bouncer after the restart",
        timeout=DECISION_TIMEOUT,
        interval=0.5,
        not_yet=(Exception,),
    )
    assert status(lab, OUTSIDE) == 200

    # An apply renders the allowlist from config.yaml.
    docker("exec", "-u", "homeproxy", lab.homeproxy, PYTHON, "-c", APPLY)
    assert status(lab, OUTSIDE) == 200

    # Emptied, the list matches nothing.
    allowlist(lab, "remove", OUTSIDE, ALLOWED)
    assert (status(lab, OUTSIDE), status(lab, ALLOWED)) == (403, 403)


@pytest.mark.usefixtures("lapi")
@pytest.mark.parametrize(
    ("bouncer_key", "results"),
    [
        pytest.param(BOUNCER_KEY, [True, True, None, True], id="right-key"),
        pytest.param("not-the-bouncer-key", [True, False, None, True], id="wrong-key"),
    ],
)
def test_the_connection_test_checks_each_part_of_a_real_engine(
    lab: Lab, bouncer_key: str, results: list[bool | None]
) -> None:
    """The lab's engine has no online API, so it holds no blocklist decision. The blocklist line
    then says 0, not that blocklists are missing."""
    crowdsec = Crowdsec.model_validate(
        {"enabled": True, "lapi_url": f"http://{lab.lapi}:8080", "syslog": f"{lab.lapi}:514"}
    )
    keys = SecretsCrowdsec.model_validate({**MACHINE, "bouncer_key": bouncer_key})

    lines = connection_check(crowdsec, keys)

    assert [line.ok for line in lines] == results
    assert lines[3].message == "0 blocklist decisions are active."
