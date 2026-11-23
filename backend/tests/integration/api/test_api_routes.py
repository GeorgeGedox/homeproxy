"""Route endpoints on the real app: edits are staged, basic auth users are write-only."""

import json
import socket
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from homeproxy.api import changes
from homeproxy.geoip.data import GeoipData, save_country_map
from homeproxy.store import Store, load_tree

ROUTES = "/api/v1/routes"


def route(name: str = "app", **fields: Any) -> dict[str, Any]:
    """A route body, `app` as the lab has it unless `fields` say otherwise."""
    body: dict[str, Any] = {
        "version": 1,
        "name": name,
        "type": "http",
        "hosts": [f"{name}.example.com"],
        "exposure": "lan",
        "upstreams": [{"address": "192.168.1.20:8080"}],
    }
    return body | fields


def yaml_route(name: str, **fields: Any) -> str:
    """A route file, as `route` builds its body."""
    return json.dumps(route(name, **fields))


def pending_kinds(api: TestClient) -> dict[str, str]:
    """The pending changes as `{path: kind}`."""
    return {c["path"]: c["kind"] for c in api.get("/api/v1/changes").json()["changes"]}


@pytest.fixture
def listener() -> Iterator[str]:
    """An upstream that accepts connections, on a random loopback port."""
    with socket.create_server(("127.0.0.1", 0)) as server:
        yield f"127.0.0.1:{server.getsockname()[1]}"


def test_a_new_route_is_staged_and_listed(lab: Path, api: TestClient) -> None:
    """POST stages the file with defaults left out, GET shows it, and a taken name is 409. A live
    route whose delete is staged is taken too, or a new route of its name would get its basic auth
    users."""
    created = api.post(ROUTES, json=route("files", exposure="public"))
    assert created.status_code == 201

    assert api.post(ROUTES, json=route("files")).json()["error"]["code"] == "route_exists"
    assert [r["name"] for r in api.get(ROUTES).json()] == ["app", "files"]
    assert api.get(f"{ROUTES}/files").json()["exposure"] == "public"
    assert not (lab / "config" / "routes" / "files.yaml").exists()
    staged = (lab / "pending" / "files" / "routes" / "files.yaml").read_text()
    assert "enabled" not in staged
    assert "websocket" not in staged
    assert api.delete(f"{ROUTES}/app").status_code == 204
    assert api.post(ROUTES, json=route("app")).json()["error"]["code"] == "route_exists"


def test_a_new_route_without_a_name_gets_one_no_live_or_pending_route_has(
    lab: Path, api: TestClient
) -> None:
    """A live route keeps its name while its delete is staged, so its basic auth users can never
    pass to a new route. The name rules are unit tests."""
    routes = lab / "config" / "routes"
    live = (routes / "app.yaml").read_text().replace("name: app", "name: app-example-com")
    (routes / "app-example-com.yaml").write_text(live)
    api.delete(f"{ROUTES}/app-example-com")
    nameless = {key: value for key, value in route().items() if key != "name"}

    created = [api.post(ROUTES, json=nameless) for _ in range(2)]

    assert [c.status_code for c in created] == [201, 201]
    names = [c.json()["route"]["name"] for c in created]
    assert names == ["app-example-com-2", "app-example-com-3"]
    assert (lab / "pending" / "files" / "routes" / "app-example-com-2.yaml").exists()


def test_an_edit_keeps_comments_and_an_unchanged_edit_stages_nothing(
    lab: Path, api: TestClient
) -> None:
    """Only changed keys are rewritten, so the hand-written comments and styles stay. The lab's
    route file has four-space indents, so a rewrite of an unchanged file would show."""
    assert api.put(f"{ROUTES}/app", json=route()).status_code == 200
    assert pending_kinds(api) == {}

    assert api.put(f"{ROUTES}/app", json=route(exposure="public")).status_code == 200

    staged = (lab / "pending" / "files" / "routes" / "app.yaml").read_text()
    assert staged.startswith("# Hand-written route.\n")
    assert "hosts: [app.example.com]\n" in staged
    assert "exposure: public" in staged
    assert "exposure: lan" in (lab / "config" / "routes" / "app.yaml").read_text()


def test_route_edits_keep_basic_auth_users_so_a_discard_still_renders(
    lab: Path, api: TestClient
) -> None:
    """Users are not pending, so a rename copies them and a delete keeps them. After either edit
    is discarded, the route still renders with its users."""
    saved = lab / "config" / "routes" / "app.yaml"
    saved.write_text(saved.read_text() + "auth: {type: basic}\n")
    users = {"users": {"alice": "long enough password"}}
    assert api.put(f"{ROUTES}/app/basic-auth", json=users).status_code == 200
    assert api.post(ROUTES, json=route("taken")).status_code == 201
    basic = {"auth": {"type": "basic"}, "hosts": ["app.example.com"]}
    clash = api.put(f"{ROUTES}/app", json=route("taken", **basic))
    assert clash.json()["error"]["code"] == "route_exists"

    assert api.put(f"{ROUTES}/app", json=route("web", **basic)).status_code == 200

    assert pending_kinds(api) == {
        "routes/app.yaml": "delete",
        "routes/taken.yaml": "write",
        "routes/web.yaml": "write",
    }
    assert api.get(f"{ROUTES}/web/basic-auth").json() == {"users": ["alice"]}
    for edit in ("rename", "delete"):
        assert api.delete("/api/v1/changes").status_code == 204
        assert api.get("/api/v1/changes").json()["generated_error"] is None, edit
        assert api.delete(f"{ROUTES}/app").status_code == 204
    assert set(Store(lab).load_secrets().basic_auth) == {"app", "web"}


def test_a_rename_onto_a_live_route_whose_delete_is_staged_keeps_its_users(
    lab: Path, api: TestClient
) -> None:
    """Its users stay until the apply, so the renamed route would get them, or overwrite them for a
    discard to bring back. Renaming back to the old name moves the same users, so it works."""
    routes = lab / "config" / "routes"
    (routes / "app.yaml").write_text((routes / "app.yaml").read_text() + "auth: {type: basic}\n")
    (routes / "old.yaml").write_text(yaml_route("old", auth={"type": "basic"}))
    for name, user in (("app", "alice"), ("old", "bob")):
        users = {"users": {user: "long enough password"}}
        assert api.put(f"{ROUTES}/{name}/basic-auth", json=users).status_code == 200
    assert api.delete(f"{ROUTES}/old").status_code == 204
    basic = {"auth": {"type": "basic"}, "hosts": ["app.example.com"]}

    clash = api.put(f"{ROUTES}/app", json=route("old", **basic))

    assert clash.json()["error"]["code"] == "route_exists"
    assert api.put(f"{ROUTES}/app", json=route("web", **basic)).status_code == 200
    assert api.put(f"{ROUTES}/web", json=route("app", **basic)).status_code == 200
    assert set(Store(lab).load_secrets().basic_auth["old"]) == {"bob"}


def test_a_broken_hand_edited_route_can_be_deleted(lab: Path, api: TestClient) -> None:
    """Delete does not validate the file, so a route that no longer parses can be removed. Only
    a missing route is 404. Saving another route beside it still works, without its warnings."""
    broken = lab / "config" / "routes" / "broken.yaml"
    broken.write_text("version: 1\nname: broken\ntype: http\nhosts: not a list\n")

    assert api.post(ROUTES, json=route("files")).json()["warnings"] == []
    assert api.delete(f"{ROUTES}/broken").status_code == 204
    assert api.delete(f"{ROUTES}/broken").status_code == 404
    assert pending_kinds(api) == {"routes/broken.yaml": "delete", "routes/files.yaml": "write"}


def test_basic_auth_users_are_write_only(lab: Path, api: TestClient) -> None:
    """Names come back, hashes and passwords never do, and refused changes write nothing."""
    password = "a long secret password"  # noqa: S105
    secrets = lab / "secrets" / "secrets.yaml"
    response = api.put(f"{ROUTES}/app/basic-auth", json={"users": {"alice": password}})
    assert response.json() == {"users": ["alice"]}
    hashed = load_tree(secrets)["basic_auth"]["app"]["alice"]
    assert hashed.startswith("$6$")
    assert api.put(ROUTES + "/app", json=route(auth={"type": "basic"})).status_code == 200
    before = secrets.read_bytes()

    short = api.put(f"{ROUTES}/app/basic-auth", json={"users": {"bob": "short"}})
    empty = api.put(f"{ROUTES}/app/basic-auth", json={"users": {}})
    new_without_password = api.put(f"{ROUTES}/app/basic-auth", json={"users": {"bob": None}})

    for refused, code in [
        (short, "invalid_basic_auth"),
        (empty, "users_required"),
        (new_without_password, "invalid_basic_auth"),
    ]:
        assert (refused.status_code, refused.json()["error"]["code"]) == (400, code)
    assert secrets.read_bytes() == before
    kept = api.put(f"{ROUTES}/app/basic-auth", json={"users": {"alice": None}})
    assert kept.json() == {"users": ["alice"]}
    shown = api.get(f"{ROUTES}/app/basic-auth").text + kept.text + short.text
    assert hashed not in shown
    assert password not in shown


@pytest.mark.parametrize(
    ("draft", "error"),
    [
        (route("new", hosts=["new.example.org"]), "new.example.org is in no configured domain"),
        (route("new", hosts=["app.example.com"]), "app.example.com is already served"),
        (
            {
                "version": 1,
                "type": "tcp",
                "listen_port": 443,
                "exposure": "public",
                "upstreams": [{"address": "192.168.1.40:25565"}],
            },
            "port 443 is already used by https_port",
        ),
        (route("new", upstreams=[{"address": "192.168.1.10:8443"}]), "is the dashboard"),
    ],
)
def test_validation_reports_what_the_apply_would_refuse(
    lab: Path, api: TestClient, draft: dict[str, Any], error: str
) -> None:
    """Each cross-file rule shows for the draft, and nothing is staged."""
    result = api.post(f"{ROUTES}/validate", json=draft).json()

    assert [e for e in result["errors"] if error in e], result
    assert pending_kinds(api) == {}


def test_validation_of_an_edited_route_checks_it_in_place(
    lab: Path, api: TestClient, listener: str
) -> None:
    """The edited route replaces its saved copy, also under a new name, so it never clashes with
    itself. A problem elsewhere in the config is not the draft's. A reachable upstream gives no
    warning, a closed one does."""
    with (lab / "config" / "config.yaml").open("a") as config:
        config.write("dns_records: [{name: nas.example.org, target: 192.168.1.30}]\n")
    reachable = route(upstreams=[{"address": listener}])
    renamed = route("web", hosts=["app.example.com"], upstreams=[{"address": "127.0.0.1:9"}])

    same = api.post(f"{ROUTES}/validate", json=reachable).json()
    moved = api.post(f"{ROUTES}/validate", params={"name": "app"}, json=renamed).json()

    assert same == {"errors": [], "warnings": []}
    assert moved == {"errors": [], "warnings": ["upstream 127.0.0.1:9 is not reachable"]}


def test_a_host_routed_only_on_paths_warns_at_validate_and_save(lab: Path, api: TestClient) -> None:
    """Validate and save both report the rule for the route's hosts. Its cases are unit tests."""
    warning = (
        "app.example.com is routed only on /api, so on the LAN its other paths get 404. Add a "
        "route for /, or move these paths to their own host."
    )
    path_route = route("api", hosts=["app.example.com"], path_prefix="/api")
    disabled = route(enabled=False, upstreams=[{"address": "127.0.0.1:9"}])

    assert api.post(ROUTES, json=path_route).json()["warnings"] == []
    checked = api.post(f"{ROUTES}/validate", json=disabled).json()
    saved = api.put(f"{ROUTES}/app", json=disabled).json()

    assert checked == {"errors": [], "warnings": ["upstream 127.0.0.1:9 is not reachable", warning]}
    assert saved == {
        "route": api.get(f"{ROUTES}/app").json(),
        "warnings": [warning],
        "site_changed": [],
    }


def test_a_route_error_names_the_field_without_the_route_type(api: TestClient) -> None:
    """The route type that leads pydantic's location is not part of `field`."""
    response = api.post(ROUTES, json=route(hosts=["not a host"]))

    assert response.status_code == 422
    assert response.json()["error"]["field"] == "hosts.0"


@pytest.mark.parametrize(
    ("method", "path", "name"), [("POST", ROUTES, "files"), ("PUT", f"{ROUTES}/app", "app")]
)
def test_a_route_block_list_is_checked_against_the_geoip_data(
    lab: Path, api: TestClient, method: str, path: str, name: str
) -> None:
    """The same check as `PUT /geoip`, at the route's own field, on create and on edit."""
    info = GeoipData(
        provider="dbip-lite", updated_at=datetime.now(UTC), entries=1, countries=["CN"]
    )
    save_country_map(lab, "1.0.0.0/24 CN\n", info)
    body = route(name, geoip={"block_countries": ["CN", "DU"]})

    response = api.request(method, path, json=body)

    assert (response.status_code, response.json()["error"]["code"]) == (422, "unknown_country")
    assert response.json()["error"]["field"] == "geoip.block_countries.1"
    assert pending_kinds(api) == {}


def test_an_edit_behind_a_held_lock_answers_busy(
    lab: Path, api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An edit waiting behind a long apply gives up with 409 instead of holding its thread."""
    monkeypatch.setattr(changes, "LOCK_WAIT", 0.2)
    responses: list[Any] = []

    def edit() -> None:
        responses.append(api.put(f"{ROUTES}/app", json=route(exposure="public")))

    with Store(lab).lock():
        worker = threading.Thread(target=edit, daemon=True)
        worker.start()
        worker.join(timeout=5.0)
        assert responses, "the edit was still waiting for the lock after 5 seconds"

    [response] = responses
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "busy"
    assert pending_kinds(api) == {}


def path_only_warning(host: str, paths: str) -> str:
    """The N-6 warning for a host routed only on `paths`."""
    return (
        f"{host} is routed only on {paths}, so on the LAN its other paths get 404. Add a route "
        "for /, or move these paths to their own host."
    )


def test_a_host_change_stages_every_route_of_its_site(lab: Path, api: TestClient) -> None:
    """Changing `www` reaches `static` on it and `app` through `api`, which has both hosts, and
    leaves `files`. Each warning shows once. The grouping cases are unit tests."""
    api.post(
        ROUTES, json=route("api", hosts=["www.example.com", "app.example.com"], path_prefix="/api")
    )
    api.post(ROUTES, json=route("static", hosts=["www.example.com"], path_prefix="/static"))
    api.post(ROUTES, json=route("files"))
    settings = {"exposure": "public", "cloudflare_dns": True}

    response = api.put(f"{ROUTES}/hosts/www.example.com", json=settings)

    assert response.status_code == 200
    changed = response.json()
    assert [r["name"] for r in changed["routes"]] == ["api", "app", "static"]
    for saved in changed["routes"]:
        assert saved["exposure"] == "public"
        assert saved["cloudflare"] == {"dns": True, "proxied": None}
        assert api.get(f"{ROUTES}/{saved['name']}").json() == saved
    assert changed["warnings"] == [path_only_warning("www.example.com", "/api, /static")]
    assert api.get(f"{ROUTES}/files").json()["exposure"] == "lan"
    staged = (lab / "pending" / "files" / "routes" / "app.yaml").read_text()
    assert staged.startswith("# Hand-written route.\n")


def test_saving_a_route_sets_its_site_settings_on_the_whole_site(
    lab: Path, api: TestClient
) -> None:
    """An edit and a create both copy exposure, Cloudflare and DNS to the other paths of the site,
    and name them. The grouping cases are unit tests."""
    api.post(ROUTES, json=route("api", hosts=["app.example.com"], path_prefix="/api"))
    public: dict[str, Any] = {
        "exposure": "public",
        "cloudflare": {"dns": True},
        "dns": {"target": "192.168.1.30"},
    }

    edited = api.put(f"{ROUTES}/app", json=route(**public)).json()
    api_route = api.get(f"{ROUTES}/api").json()
    created = api.post(
        ROUTES, json=route("static", hosts=["app.example.com"], path_prefix="/static")
    ).json()

    assert edited["site_changed"] == ["app.example.com/api"]
    assert {key: api_route[key] for key in public} == {
        "exposure": "public",
        "cloudflare": {"dns": True, "proxied": None},
        "dns": {"target": "192.168.1.30"},
    }
    assert sorted(created["site_changed"]) == ["app.example.com", "app.example.com/api"]
    assert api.get(f"{ROUTES}/app").json()["exposure"] == "lan"
    assert api.put(f"{ROUTES}/app", json=route()).json()["site_changed"] == []


def test_an_unchanged_save_mends_a_site_a_hand_edit_split(lab: Path, api: TestClient) -> None:
    """The route itself stages nothing, and the other path of its site takes its exposure."""
    split = route("api", hosts=["app.example.com"], path_prefix="/api", exposure="public")
    (lab / "config" / "routes" / "api.yaml").write_text(json.dumps(split))

    saved = api.put(f"{ROUTES}/app", json=route()).json()

    assert saved["site_changed"] == ["app.example.com/api"]
    assert pending_kinds(api) == {"routes/api.yaml": "write"}
    assert api.get(f"{ROUTES}/api").json()["exposure"] == "lan"


def test_validation_checks_a_draft_with_its_site_settings_on_the_whole_site(
    lab: Path, api: TestClient
) -> None:
    """A draft that changes the site's exposure does not clash with the paths it would change."""
    api.post(ROUTES, json=route("api", hosts=["app.example.com"], path_prefix="/api"))
    draft = route("api", hosts=["app.example.com"], path_prefix="/api", exposure="public")

    result = api.post(f"{ROUTES}/validate", json=draft).json()

    assert result["errors"] == []
    assert pending_kinds(api) == {"routes/api.yaml": "write"}


@pytest.mark.parametrize(
    ("file", "path", "body", "status", "field"),
    [
        ("web.yaml", "hosts/app.example.com", {"exposure": "public"}, 422, "name"),
        ("web_copy.yaml", "hosts/app.example.com", {"exposure": "public"}, 409, None),
        ("web_copy.yaml", "app", route(exposure="public"), 409, None),
    ],
    ids=["name-not-its-file", "file-name-not-a-route-name", "route-save"],
)
def test_a_host_change_one_route_fails_stages_no_route(
    lab: Path,
    api: TestClient,
    file: str,
    path: str,
    body: dict[str, Any],
    status: int,
    field: str | None,
) -> None:
    """Every file is checked before the first is staged, on a host change and on a route save that
    changes its site. Each file is a hand copy that loads but cannot be staged, and it sorts after
    `app.yaml`."""
    copied = route("web-copy", hosts=["app.example.com"], path_prefix="/api")
    (lab / "config" / "routes" / file).write_text(json.dumps(copied))

    response = api.put(f"{ROUTES}/{path}", json=body)

    assert response.status_code == status
    assert response.json()["error"]["field"] == field
    assert pending_kinds(api) == {}


@pytest.mark.parametrize(
    ("host", "settings", "status"),
    [
        ("nas.example.com", {"exposure": "public"}, 404),
        ("app.example.com", {}, 422),
        ("app.example.com", {"exposure": "lan"}, 200),
    ],
    ids=["unknown-host", "no-setting", "no-change"],
)
def test_a_host_change_that_changes_nothing_stages_nothing(
    lab: Path, api: TestClient, host: str, settings: dict[str, Any], status: int
) -> None:
    """Nothing is staged for a host no route has, a body that sets nothing, or a value the
    routes already have. The lab's `app.yaml` has four-space indents, so a rewrite would show."""
    response = api.put(f"{ROUTES}/hosts/{host}", json=settings)

    assert response.status_code == status
    assert pending_kinds(api) == {}


def test_the_hosts_routed_only_on_paths_are_listed(lab: Path, api: TestClient) -> None:
    """The page shows them without a save. A host with a `/` route has none."""
    api.post(ROUTES, json=route("test", hosts=["www.example.com"], path_prefix="/test"))
    api.post(ROUTES, json=route("api", hosts=["app.example.com"], path_prefix="/api"))

    warnings = api.get(f"{ROUTES}/warnings")

    assert warnings.json() == [
        {"host": "www.example.com", "message": path_only_warning("www.example.com", "/test")}
    ]
