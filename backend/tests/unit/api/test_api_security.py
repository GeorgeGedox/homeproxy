"""`/security` on the real app: decisions against a fake LAPI, and the live allowlist.

The LAPI client itself is tested against a real CrowdSec in `tests/integration/crowdsec`. A fake
LAPI here stands in for it, so these tests only check that the endpoints reach the client, map its
errors and never return the machine credentials. No HAProxy runs, so an allowlist edit that needs
one shows the HAProxy failure answer.
"""

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from homeproxy.store import Store, load_tree

MACHINE_ID = "homeproxy-ui"
MACHINE_PASSWORD = "api-test-machine-password"  # noqa: S105 - accepted by the fake LAPI
TOKEN = "api-test-lapi-token"  # noqa: S105
COMMUNITY_BAN = {
    "id": 7,
    "type": "ban",
    "scope": "Ip",
    "value": "203.0.113.7",
    "origin": "CAPI",
    "scenario": "crowdsecurity/http-probing",
    "duration": "3h59m50s",
}


class FakeLapi:
    """Answers the four LAPI calls the client makes, for one machine."""

    def __init__(self) -> None:
        self.decisions: list[dict[str, Any]] = [dict(COMMUNITY_BAN)]
        self.added: list[dict[str, Any]] = []
        self.listing_fails = False

    def answer(self, method: str, path: str, body: Any, auth: str) -> tuple[int, Any]:
        if path == "/v1/watchers/login":
            ok = body == {"machine_id": MACHINE_ID, "password": MACHINE_PASSWORD}
            return (200, {"token": TOKEN}) if ok else (401, {"message": "incorrect Username"})
        if auth != f"Bearer {TOKEN}":
            return 401, {"message": "no token"}
        if method == "GET":
            if self.listing_fails:
                return 500, {"message": "listing timed out"}
            return 200, [{"decisions": self.decisions}]
        if method == "POST":
            self.added += body
            return 201, ["1"]
        decision_id = int(path.rsplit("/", 1)[1])
        if decision_id not in [d["id"] for d in self.decisions]:
            return 500, {"message": "decision with id 99 not found"}
        self.decisions = [d for d in self.decisions if d["id"] != decision_id]
        return 200, {"nbDeleted": "1"}


@pytest.fixture
def lapi() -> Iterator[tuple[FakeLapi, str]]:
    """A fake LAPI on a random loopback port, and its URL."""
    fake = FakeLapi()

    class Handler(BaseHTTPRequestHandler):
        def _handle(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length)) if length else None
            path = self.path.split("?", 1)[0]
            status, answer = fake.answer(
                self.command, path, body, self.headers.get("Authorization", "")
            )
            data = json.dumps(answer).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = do_DELETE = _handle

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield fake, f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


def enable_crowdsec(data_dir: Path, lapi_url: str, password: str = MACHINE_PASSWORD) -> None:
    with (data_dir / "config" / "config.yaml").open("a") as config:
        config.write(f"crowdsec:\n  enabled: true\n  lapi_url: {lapi_url}\n")
        config.write("  syslog: 127.0.0.1:5140\n")
    with (data_dir / "secrets" / "secrets.yaml").open("a") as secrets:
        secrets.write(f"crowdsec:\n  machine_id: {MACHINE_ID}\n  machine_password: {password}\n")


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


def test_decision_counts_name_only_local_ones_and_whether_a_blocklist_is_loaded(
    data_dir: Path, lapi: tuple[FakeLapi, str], api: TestClient
) -> None:
    fake, url = lapi
    enable_crowdsec(data_dir, url)
    local = {**COMMUNITY_BAN, "origin": "crowdsec"}
    fake.decisions += [{**local, "id": 8}, {**local, "id": 9, "type": "captcha"}]

    counts = api.get("/api/v1/security/decisions/counts")

    assert counts.json() == {"bans": 1, "captchas": 1, "blocklist": True}


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


def test_an_allowlist_edit_is_refused_while_changes_are_pending(
    data_dir: Path, live_release: Path, api: TestClient
) -> None:
    settings = api.get("/api/v1/settings").json()
    settings["dns_server"]["forward_unknown_to"] = ["192.168.1.1"]
    assert api.put("/api/v1/settings", json=settings).json()["changed"] is True

    response = api.post("/api/v1/security/allowlist", json={"entry": "203.0.113.7"})

    assert (response.status_code, response.json()["error"]["code"]) == (409, "changes_pending")
    assert api.get("/api/v1/security/allowlist").json() == []


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
