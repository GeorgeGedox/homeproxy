"""`/security` on the real app: decisions against a fake LAPI, and the live allowlist.

The LAPI client itself is tested against a real CrowdSec in `tests/integration/crowdsec`. A fake
LAPI here stands in for it, so these tests only check that the endpoints reach the client, map its
errors and never return the machine credentials. No HAProxy runs, so an allowlist edit that needs
one shows the HAProxy failure answer.
"""

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from homeproxy.store import Store, load_tree
from tests.unit.api.conftest import (
    BOUNCER_KEY,
    COMMUNITY_BAN,
    MACHINE_ID,
    MACHINE_PASSWORD,
    PULLED_AT,
    FakeLapi,
    enable_crowdsec,
    stage_config,
    stage_route,
)


def actions(data_dir: Path) -> list[tuple[str, dict[str, Any]]]:
    return [(e.action, dict(e.details)) for e in Store(data_dir).audit.read()][::-1]


def test_decisions_are_listed_added_and_deleted(
    data_dir: Path, lapi: tuple[FakeLapi, str], api: TestClient
) -> None:
    """Each call reaches the LAPI as the machine, each change is audited, and no answer holds
    the machine credentials."""
    fake, url = lapi
    enable_crowdsec(data_dir, url)

    listed = api.get("/api/v1/security/decisions")
    added = api.post(
        "/api/v1/security/decisions",
        json={"value": "198.51.100.0/24", "type": "captcha", "duration_seconds": 3600},
    )
    deleted = api.delete("/api/v1/security/decisions/7")

    assert [d["value"] for d in listed.json()] == ["203.0.113.7"]
    assert listed.json()[0]["origin"] == "CAPI"
    assert (added.status_code, deleted.status_code) == (204, 204)
    [decision] = fake.added[0]["decisions"]
    assert (decision["scope"], decision["value"], decision["type"], decision["duration"]) == (
        "Range",
        "198.51.100.0/24",
        "captcha",
        "3600s",
    )
    assert fake.decisions == []
    assert actions(data_dir) == [
        (
            "security.decision.add",
            {"value": "198.51.100.0/24", "type": "captcha", "duration_seconds": 3600},
        ),
        (
            "security.decision.delete",
            {"id": 7, "value": "203.0.113.7", "type": "ban", "origin": "CAPI"},
        ),
    ]
    for response in (listed, added, deleted):
        assert MACHINE_PASSWORD not in response.text
        assert MACHINE_ID not in response.text


def test_an_unban_goes_through_when_the_decision_cannot_be_looked_up(
    data_dir: Path, lapi: tuple[FakeLapi, str], api: TestClient
) -> None:
    """The lookup only names the address in the audit log. Listing every blocklist decision can
    fail or time out where the delete of one does not."""
    fake, url = lapi
    enable_crowdsec(data_dir, url)
    fake.listing_fails = True

    assert api.delete("/api/v1/security/decisions/7").status_code == 204

    assert fake.decisions == []
    assert actions(data_dir) == [("security.decision.delete", {"id": 7})]


def test_decision_counts_split_local_ones_from_the_blocklist(
    data_dir: Path, lapi: tuple[FakeLapi, str], api: TestClient
) -> None:
    """The blocklist count is 0, not missing, when no blocklist decision is active. Its update is
    the time of the newest pull that brought decisions."""
    fake, url = lapi
    enable_crowdsec(data_dir, url)
    local = {**COMMUNITY_BAN, "origin": "crowdsec"}
    fake.decisions += [{**local, "id": 8}, {**local, "id": 9, "type": "captcha"}]

    loaded = api.get("/api/v1/security/decisions/counts").json()
    fake.decisions = fake.decisions[1:]
    none = api.get("/api/v1/security/decisions/counts").json()

    assert loaded == {"bans": 1, "captchas": 1, "blocklist": 1, "blocklist_updated_at": PULLED_AT}
    assert none == {"bans": 1, "captchas": 1, "blocklist": 0, "blocklist_updated_at": None}


@pytest.mark.parametrize(
    ("setup", "results"),
    [
        pytest.param({"appsec": True}, [True, True, True, True], id="all-good"),
        pytest.param({}, [True, True, None, True], id="appsec-off"),
        pytest.param({"password": "wrong", "appsec": True}, [False, True, True, False], id="login"),
        pytest.param(
            {"bouncer_key": "wrong", "appsec": True}, [True, False, False, True], id="key"
        ),
        pytest.param({"lapi_url": "http://127.0.0.1:9"}, [False, False, None, False], id="no-lapi"),
    ],
)
def test_the_connection_test_reports_each_part_on_its_own_line(
    data_dir: Path,
    lapi: tuple[FakeLapi, str],
    api: TestClient,
    setup: dict[str, Any],
    results: list[bool | None],
) -> None:
    """`docs/crowdsec.md`: the machine login, the bouncer key, AppSec and the blocklist. A part
    that fails does not stop the others, and no line holds a key or a password."""
    enable_crowdsec(data_dir, **({"lapi_url": lapi[1]} | setup))

    response = api.post("/api/v1/security/crowdsec/check")

    lines = response.json()
    assert [(line["part"], line["ok"]) for line in lines] == list(
        zip(["machine", "bouncer", "appsec", "blocklist"], results, strict=True)
    )
    assert all(line["message"] for line in lines)
    for secret in (MACHINE_PASSWORD, BOUNCER_KEY, "wrong"):
        assert secret not in response.text


def test_decisions_need_crowdsec_on(api: TestClient) -> None:
    response = api.get("/api/v1/security/decisions")

    assert (response.status_code, response.json()["error"]["code"]) == (
        409,
        "crowdsec_not_configured",
    )


@pytest.mark.parametrize(
    ("call", "status", "code"),
    [
        # A value the client refuses after the model accepted it.
        (("post", "/security/decisions", {"value": "::ffff:192.0.2.1", "type": "ban"}), 422, None),
        (("post", "/security/decisions", {"value": "0.0.0.0/0", "type": "ban"}), 422, None),
        (("delete", "/security/decisions/99", None), 502, "crowdsec_error"),
    ],
)
def test_refused_decisions_change_nothing(
    data_dir: Path,
    lapi: tuple[FakeLapi, str],
    api: TestClient,
    call: tuple[str, str, dict[str, Any] | None],
    status: int,
    code: str | None,
) -> None:
    fake, url = lapi
    enable_crowdsec(data_dir, url)
    method, path, body = call
    if body is not None:
        body["duration_seconds"] = 60

    response = api.request(method, f"/api/v1{path}", json=body)

    assert response.status_code == status, response.text
    assert code is None or response.json()["error"]["code"] == code
    assert (fake.added, len(fake.decisions), actions(data_dir)) == ([], 1, [])


def test_a_lapi_failure_never_shows_the_password(
    data_dir: Path, lapi: tuple[FakeLapi, str], api: TestClient
) -> None:
    enable_crowdsec(data_dir, lapi[1], password="wrong-machine-password")  # noqa: S106

    response = api.get("/api/v1/security/decisions")

    assert (response.status_code, response.json()["error"]["code"]) == (502, "crowdsec_error")
    assert "wrong-machine-password" not in response.text


@pytest.fixture
def live_release(data_dir: Path) -> Path:
    """A live release that loads `allow.lst`, as the generator writes it with CrowdSec off."""
    release = data_dir / "releases" / "r1"
    release.mkdir(parents=True)
    (release / "allow.lst").write_text("")
    (data_dir / "generated").symlink_to("releases/r1")
    return release


def test_allowlist_edits_are_saved_and_live_without_an_apply(
    data_dir: Path, live_release: Path, api: TestClient
) -> None:
    """Add and remove reach `config.yaml`, with its comments, and the live `allow.lst`. A range
    goes in the path with its slash, and both edits are audited. A public range is a warning."""
    assert api.get("/api/v1/security/allowlist").json() == []

    single = api.post("/api/v1/security/allowlist", json={"entry": "203.0.113.7"})
    added = api.post("/api/v1/security/allowlist", json={"entry": "2001:db8::/32"})
    removed = api.delete("/api/v1/security/allowlist/203.0.113.7")

    assert single.json()["warnings"] == []
    assert added.json() == {
        "allowlist": ["203.0.113.7", "2001:db8::/32"],
        "warnings": [
            "crowdsec.allowlist: 2001:db8::/32 is a public range, so CrowdSec never checks its "
            "clients"
        ],
    }
    assert removed.json() == ["2001:db8::/32"]
    assert api.get("/api/v1/security/allowlist").json() == ["2001:db8::/32"]
    assert (live_release / "allow.lst").read_text() == "2001:db8::/32\n"
    config = (data_dir / "config" / "config.yaml").read_text()
    assert "# Hand-written comment that every edit keeps." in config
    assert load_tree(data_dir / "config" / "config.yaml")["crowdsec"]["allowlist"] == [
        "2001:db8::/32"
    ]
    assert api.delete("/api/v1/security/allowlist/2001:db8::/32").json() == []
    assert [action for action, _ in actions(data_dir)] == [
        "security.allowlist.add",
        "security.allowlist.add",
        "security.allowlist.remove",
        "security.allowlist.remove",
    ]
    assert api.get("/api/v1/changes").json()["changes"] == []


def test_an_allowlist_edit_waits_only_for_a_staged_config(
    live_release: Path, api: TestClient
) -> None:
    """A staged `config.yaml` holds the old allowlist, so an apply would undo an edit. A staged
    route does not touch `config.yaml`, so it does not block the edit."""
    stage_route(api)
    assert api.post("/api/v1/security/allowlist", json={"entry": "203.0.113.7"}).is_success
    stage_config(api)

    response = api.post("/api/v1/security/allowlist", json={"entry": "198.51.100.9"})

    assert (response.status_code, response.json()["error"]["code"]) == (409, "changes_pending")
    assert api.get("/api/v1/security/allowlist").json() == ["203.0.113.7"]


def test_an_allowlist_entry_that_is_not_an_address_is_refused(
    live_release: Path, api: TestClient
) -> None:
    posted = api.post("/api/v1/security/allowlist", json={"entry": "1.2.3.4\nacl x"})
    deleted = api.delete("/api/v1/security/allowlist/0.0.0.0/0")

    assert (posted.status_code, deleted.status_code) == (422, 422)
    assert (live_release / "allow.lst").read_text() == ""


def test_an_allowlist_edit_haproxy_misses_is_saved_and_says_so(
    data_dir: Path, live_release: Path, api: TestClient
) -> None:
    """With CrowdSec on in the live release, HAProxy must take the change. It is not running, so
    the answer is 502, the change is saved, and the live file waits for the retry."""
    (live_release / "crowdsec-spoe.cfg").write_text("")

    response = api.post("/api/v1/security/allowlist", json={"entry": "203.0.113.7"})

    assert (response.status_code, response.json()["error"]["code"]) == (502, "haproxy_error")
    assert "saved" in response.json()["error"]["message"]
    assert api.get("/api/v1/security/allowlist").json() == ["203.0.113.7"]
    assert (live_release / "allow.lst").read_text() == ""
