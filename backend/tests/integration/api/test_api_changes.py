"""`/changes` and `/apply` on the real app: diffs without secrets, conflicts, discard, failures."""

from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from homeproxy.auth.passwords import hash_password
from homeproxy.generator import USERLISTS_CONFIG
from homeproxy.geoip.data import GeoipData, save_country_map
from homeproxy.store import Store, load_tree
from homeproxy.store.pending import PendingChanges
from homeproxy.store.tree import dump_tree
from tests.integration.api.conftest import CLOUDFLARE_TOKEN

CHANGES = "/api/v1/changes"
ROUTE = {
    "version": 1,
    "name": "app",
    "type": "http",
    "hosts": ["app.example.com"],
    "exposure": "lan",
    "upstreams": [{"address": "192.168.1.20:8080"}],
    "auth": {"type": "basic"},
    "request_headers": {"X-Token": CLOUDFLARE_TOKEN},
}


def stage_new_password_hash(root: Path) -> tuple[str, str]:
    """Stage `config.yaml` with another password hash, as a restore of an old snapshot could
    before M1-5's fix. Return the live and the staged hash."""
    store = Store(root)
    tree = load_tree(store.config_path)
    live = str(tree["dashboard"]["password_hash"])
    staged = hash_password("another password entirely")
    tree["dashboard"]["password_hash"] = staged
    with store.lock() as held:
        PendingChanges(store).stage("config.yaml", dump_tree(tree, "config.yaml"), held)
    return live, staged


def test_the_review_diffs_never_show_a_secret_or_a_hash(lab: Path, api: TestClient) -> None:
    """The summary and the YAML and HAProxy diffs show the change, but no password hash, no basic
    auth hash, no registered secret, and not the userlist file."""
    users = {"users": {"alice": "long enough password"}}
    assert api.put("/api/v1/routes/app/basic-auth", json=users).status_code == 200
    assert api.put("/api/v1/routes/app", json=ROUTE).status_code == 200
    hashes = stage_new_password_hash(lab)
    basic_hash = load_tree(lab / "secrets" / "secrets.yaml")["basic_auth"]["app"]["alice"]

    review = api.get(CHANGES).json()

    assert [c["path"] for c in review["changes"]] == ["config.yaml", "routes/app.yaml"]
    assert review["summary"] == [{"kind": "route", "action": "changed", "label": "app.example.com"}]
    assert review["generated_error"] is None
    assert "+  X-Token: [REDACTED]" in review["config_diff"]
    assert "http-request auth" in review["generated_diff"]
    for secret in (*hashes, basic_hash, CLOUDFLARE_TOKEN, USERLISTS_CONFIG, "$argon2", "$6$"):
        assert secret not in review["config_diff"]
        assert secret not in review["generated_diff"]
        assert secret not in str(review["summary"])


def test_a_block_list_edit_shows_in_the_config_diff_only(lab: Path, api: TestClient) -> None:
    """The country map it changes can be about 100k lines, so the generated diff leaves it out."""
    info = GeoipData(
        provider="dbip-lite", updated_at=datetime.now(UTC), entries=1, countries=["CN"]
    )
    save_country_map(lab, "1.0.0.0/24 CN\n", info)
    public = ROUTE | {"exposure": "public", "auth": {"type": "none"}}
    assert api.put("/api/v1/routes/app", json=public).status_code == 200
    assert api.put("/api/v1/geoip", json={"block_countries": ["CN"]}).status_code == 204

    review = api.get(CHANGES).json()

    assert "+  block_countries:" in review["config_diff"]
    assert "country-block.map" in review["generated_diff"]
    assert "1.0.0.0/24" not in review["generated_diff"]


def test_a_config_that_cannot_render_still_shows_its_changes(lab: Path, api: TestClient) -> None:
    """A basic auth route without users cannot render. The YAML diff and the reason still show,
    so the admin can fix or discard it."""
    assert api.put("/api/v1/routes/app", json=ROUTE).status_code == 200

    review = api.get(CHANGES).json()

    assert review["generated_diff"] is None
    assert "app" in review["generated_error"]
    assert review["summary"] == [{"kind": "route", "action": "changed", "label": "app.example.com"}]
    assert "+auth:" in review["config_diff"]


def test_a_basic_auth_user_change_shows_in_the_summary_alone(lab: Path, api: TestClient) -> None:
    """Users are saved live, so nothing is staged, but the summary says the next apply changes the
    route's users."""
    route = lab / "config" / "routes" / "app.yaml"
    route.write_text(route.read_text() + "auth: {type: basic}\n")
    users = {"users": {"alice": "long enough password"}}
    assert api.put("/api/v1/routes/app/basic-auth", json=users).status_code == 200

    review = api.get(CHANGES).json()

    assert review["changes"] == []
    assert review["summary"] == [
        {"kind": "basic_auth", "action": "changed", "label": "app.example.com"}
    ]


def test_a_hand_edit_shows_as_a_conflict_that_discard_clears(lab: Path, api: TestClient) -> None:
    """A staged file changed by hand is named, and discard leaves `/data/config` as the hand edit
    left it."""
    assert api.put("/api/v1/routes/app", json=ROUTE | {"auth": {"type": "none"}}).status_code == 200
    hand_edited = lab / "config" / "routes" / "app.yaml"
    hand_edited.write_text("# edited by hand\n")

    review = api.get(CHANGES).json()
    assert (review["conflict"], review["conflicting_paths"]) == (True, ["routes/app.yaml"])

    assert api.delete(CHANGES).status_code == 204
    assert api.get(CHANGES).json()["changes"] == []
    assert hand_edited.read_text() == "# edited by hand\n"


def test_a_failed_apply_reports_its_stage_and_keeps_the_changes(lab: Path, api: TestClient) -> None:
    """An apply the cross-file rules refuse answers with the stage and the error, and the edit
    stays pending. HAProxy is never reached."""
    outside = ROUTE | {
        "hosts": ["app.example.org"],
        "auth": {"type": "none"},
        "request_headers": {},
    }
    assert api.put("/api/v1/routes/app", json=outside).status_code == 200

    result = api.post("/api/v1/apply").json()

    assert (result["success"], result["stage"]) == (False, "validate")
    assert result["errors"] == ["app.example.org is in no configured domain"]
    assert result["certificate_jobs"] == []
    assert [c["path"] for c in api.get(CHANGES).json()["changes"]] == ["routes/app.yaml"]
