"""Reconcile of route records against a fake Cloudflare, with the seeded example routes.

The examples have public routes with the switch on in both zones (`jellyfin`, `whoami` and the
passthrough `git`), LAN routes, a tcp route, and `whoami` with an unset `proxied`, which the two
zones' `default_proxied` resolve differently.
"""

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from homeproxy.cloudflare import (
    Change,
    Conflict,
    ReconcileError,
    adopt_host,
    purge,
    reconcile_store,
)
from homeproxy.crowdsec import add_to_allowlist
from homeproxy.haproxy.client import HAProxyClient
from homeproxy.store import Store, load_tree, write_tree
from tests.unit.cloudflare.conftest import FakeCloudflare, ok, snapshot

EXPECTED = {
    "example.com": {
        "jellyfin.example.com": ("home.example.com", True, "managed-by:homeproxy route:jellyfin"),
        "whoami.example.com": ("home.example.com", True, "managed-by:homeproxy route:whoami"),
    },
    "example.net": {
        "whoami.example.net": ("home.example.net", False, "managed-by:homeproxy route:whoami"),
        "git.example.net": ("home.example.net", False, "managed-by:homeproxy route:git"),
    },
}


def state(fake: FakeCloudflare, zone: str) -> dict[str, tuple[Any, ...]]:
    return {r["name"]: (r["content"], r["proxied"], r["comment"]) for r in fake.zone(zone).values()}


@pytest.fixture
def unreachable(store: Store, tmp_path: Path) -> HAProxyClient:
    """A client no HAProxy answers, and a live release for allowlist edits. CrowdSec is off in
    the examples, so an allowlist edit writes only the files."""
    (store.root / "releases" / "live").mkdir(parents=True)
    (store.root / "releases" / "live" / "allow.lst").write_bytes(b"")
    (store.root / "generated").symlink_to("releases/live")
    missing = str(tmp_path / "missing.sock")
    return HAProxyClient(missing, missing)


def set_route(store: Store, name: str, key: str, value: object) -> None:
    tree = load_tree(store.route_path(name))
    tree[key] = value
    write_tree(store.route_path(name), tree)


def test_public_routes_get_cnames_to_the_ddns_record(
    cloudflare: FakeCloudflare, store: Store
) -> None:
    results = reconcile_store(store)

    assert [r.errors for r in results] == [[], []]
    assert {zone: state(cloudflare, zone) for zone in EXPECTED} == EXPECTED
    assert [r.changes for r in reconcile_store(store)] == [[], []]


def test_a_domain_on_another_provider_gets_no_records(
    cloudflare: FakeCloudflare, store: Store
) -> None:
    """Its credentials hold no Cloudflare token, and its zone is never called."""
    tree = load_tree(store.config_path)
    tree["domains"][1].update({"dns_provider": "route53", "credentials": "r53"})
    write_tree(store.config_path, tree)
    secrets = load_tree(store.secrets_path)
    env = {
        "AWS_ACCESS_KEY_ID": "AKIAEXAMPLE",
        "AWS_SECRET_ACCESS_KEY": "reconcile-test-aws-secret-key",
    }
    secrets["dns_credentials"]["r53"] = {"provider": "route53", "env": env}
    write_tree(store.secrets_path, secrets)
    snapshot(store)

    results = reconcile_store(store)

    assert [(r.zone, r.errors) for r in results] == [("example.com", [])]
    assert state(cloudflare, "example.com") == EXPECTED["example.com"]
    assert not [call for call in cloudflare.calls if "example.net" in call]


def test_owned_records_follow_the_routes(cloudflare: FakeCloudflare, store: Store) -> None:
    gone = cloudflare.add("example.com", "old.example.com", "managed-by:homeproxy route:old")
    stale = cloudflare.add(
        "example.com", "jellyfin.example.com", "managed-by:homeproxy route:jellyfin", proxied=False
    )
    # A passthrough route in a zone whose default_proxied is true, and a host on the DDNS record.
    set_route(store, "pve", "hosts", ["pve.example.com", "home.example.com"])
    set_route(store, "pve", "exposure", "public")
    set_route(store, "pve", "cloudflare", {"dns": True})
    # A redirect route gets its record as an http route does.
    store.route_path("www").write_text(
        "version: 1\nname: www\ntype: redirect\nhosts: [www.example.com]\nexposure: public\n"
        "redirect: {to: 'https://example.com'}\ncloudflare: {dns: true}\n"
    )
    snapshot(store)

    reconcile_store(store)

    assert gone not in cloudflare.zone("example.com")
    assert cloudflare.zone("example.com")[stale]["proxied"] is True
    assert state(cloudflare, "example.com")["pve.example.com"][1] is False
    assert "home.example.com" not in state(cloudflare, "example.com")
    www = ("home.example.com", True, "managed-by:homeproxy route:www")
    assert state(cloudflare, "example.com")["www.example.com"] == www


def test_routes_on_one_host_by_path_keep_one_record(
    cloudflare: FakeCloudflare, store: Store
) -> None:
    store.route_path("jellyfin-api").write_text(
        "version: 1\nname: jellyfin-api\ntype: http\nhosts: [jellyfin.example.com]\n"
        "exposure: public\nupstreams:\n  - address: 192.168.1.21:80\npath_prefix: /api\n"
        "cloudflare: {dns: true, proxied: true}\n"
    )
    snapshot(store)

    reconcile_store(store)

    names = [record["name"] for record in cloudflare.zone("example.com").values()]
    assert names.count("jellyfin.example.com") == 1
    assert [r.changes for r in reconcile_store(store)] == [[], []]


@pytest.mark.parametrize(
    ("route", "key", "value", "host"),
    [
        pytest.param("git", "exposure", "lan", "git.example.net", id="made-lan"),
        pytest.param("jellyfin", "cloudflare", {"dns": False}, "jellyfin.example.com", id="off"),
        pytest.param("whoami", "enabled", False, "whoami.example.com", id="disabled"),
    ],
)
def test_route_that_no_longer_wants_a_record_loses_it(
    cloudflare: FakeCloudflare, store: Store, route: str, key: str, value: object, host: str
) -> None:
    reconcile_store(store)
    set_route(store, route, key, value)
    snapshot(store)

    reconcile_store(store)

    zone = host.split(".", 1)[1]
    assert host not in state(cloudflare, zone)


def test_hand_made_records_are_never_touched(cloudflare: FakeCloudflare, store: Store) -> None:
    hand_made = {
        cloudflare.add("example.com", "jellyfin.example.com", content="192.168.1.20", type="A"),
        cloudflare.add("example.com", "whoami.example.com", "my own record"),
        cloudflare.add("example.com", "nas.example.com", content="192.168.1.30", type="A"),
        cloudflare.add("example.net", "home.example.net", "managed-by:homeproxy ddns:example.net"),
        cloudflare.add("example.net", "notes.example.net", "managed-by:homeproxyish"),
        cloudflare.add("example.net", "copy.example.net", "from managed-by:homeproxy route:x"),
    }
    before = {zone: dict(cloudflare.zone(zone)) for zone in EXPECTED}

    results = reconcile_store(store)
    purge(store.load_config(), store.load_secrets())

    assert results[0].conflicts == [
        Conflict("jellyfin.example.com", "jellyfin"),
        Conflict("whoami.example.com", "whoami"),
    ]
    assert {zone: dict(cloudflare.zone(zone)) for zone in EXPECTED} == before
    assert not [call for call in cloudflare.writes() if call.rsplit("/", 1)[1] in hand_made]


@pytest.mark.parametrize("kind", ["A", "AAAA"])
def test_adopting_an_address_record_is_replaced_by_the_cname_on_the_next_reconcile(
    cloudflare: FakeCloudflare, store: Store, kind: str
) -> None:
    host = "jellyfin.example.com"
    mine = cloudflare.add("example.com", host, "my own", content="192.168.1.20", type=kind)

    route = adopt_host(store.load_config(), store.load_routes(), store.load_secrets(), host)

    assert route == "jellyfin"
    assert cloudflare.writes() == [f"PUT /zones/id-example.com/dns_records/{mine}"]
    record = cloudflare.zone("example.com")[mine]
    assert (record["type"], record["content"]) == (kind, "192.168.1.20")
    assert record["comment"] == "managed-by:homeproxy route:jellyfin"
    cloudflare.calls.clear()

    results = reconcile_store(store)

    assert results[0].errors == []
    assert [c.action for c in results[0].changes if c.name == host] == ["delete", "create"]
    writes = cloudflare.writes()
    assert writes[0] == f"DELETE /zones/id-example.com/dns_records/{mine}"
    assert [w for w in writes if w.startswith("DELETE")] == writes[:1]
    assert not [w for w in writes if w.startswith("PUT")]
    assert state(cloudflare, "example.com")[host] == (
        "home.example.com", True, "managed-by:homeproxy route:jellyfin",
    )  # fmt: skip
    assert [r["type"] for r in cloudflare.zone("example.com").values() if r["name"] == host] == [
        "CNAME"
    ]


def test_adopting_a_cname_is_updated_in_place_without_a_gap(
    cloudflare: FakeCloudflare, store: Store
) -> None:
    host = "jellyfin.example.com"
    mine = cloudflare.add("example.com", host, "my own", content="other.example.org")
    adopt_host(store.load_config(), store.load_routes(), store.load_secrets(), host)
    cloudflare.calls.clear()

    results = reconcile_store(store)

    assert results[0].errors == []
    assert not [w for w in cloudflare.writes() if w.startswith("DELETE")]
    assert cloudflare.zone("example.com")[mine]["content"] == "home.example.com"


@pytest.mark.parametrize(
    ("existing", "host", "message"),
    [
        pytest.param([], "jellyfin.example.com", "no record", id="no-record"),
        pytest.param(
            [("A", "mine"), ("A", "mine too")],
            "jellyfin.example.com",
            "2 records",
            id="two-hand-made",
        ),
        pytest.param(
            [("A", "mine"), ("CNAME", "managed-by:homeproxy route:jellyfin")],
            "jellyfin.example.com",
            "2 records",
            id="hand-made-next-to-owned",
        ),
        pytest.param(
            [("CNAME", "managed-by:homeproxy route:jellyfin")],
            "jellyfin.example.com",
            "already",
            id="already-owned",
        ),
        pytest.param(
            [("A", "managed-by:homeproxy ddns:example.com")],
            "jellyfin.example.com",
            "already",
            id="ddns-owned",
        ),
        pytest.param([("TXT", "mine")], "jellyfin.example.com", "TXT", id="record-type"),
        pytest.param(
            [("A", "mine")], "nas.example.com", "no applied public route", id="no-route-wants-it"
        ),
        pytest.param(
            [("A", "mine")], "git.example.org", "no applied public route", id="outside-domains"
        ),
    ],
)
def test_adopt_refuses_unless_one_hand_made_record_sits_on_a_route_host(
    cloudflare: FakeCloudflare,
    store: Store,
    existing: list[tuple[str, str]],
    host: str,
    message: str,
) -> None:
    for kind, comment in existing:
        cloudflare.add("example.com", host, comment, type=kind, content="192.168.1.20")
    before = dict(cloudflare.zone("example.com"))

    with pytest.raises(ReconcileError, match=message):
        adopt_host(store.load_config(), store.load_routes(), store.load_secrets(), host)

    assert cloudflare.writes() == []
    assert cloudflare.zone("example.com") == before


def test_nested_domain_keeps_the_record_in_the_parent_zone(
    cloudflare: FakeCloudflare, store: Store
) -> None:
    # sub.example.com is a domain for its certificate only, not a Cloudflare zone of its own.
    tree = load_tree(store.config_path)
    tree["domains"].append(
        {"zone": "sub.example.com", "dns_provider": "cloudflare", "credentials": "cf-main"}
    )
    write_tree(store.config_path, tree)
    (store.routes_dir / "sub.yaml").write_text(
        "version: 1\nname: sub\ntype: http\nhosts: [a.sub.example.com]\nexposure: public\n"
        "upstreams:\n  - address: 192.168.1.50:80\ncloudflare: {dns: true}\n"
    )
    snapshot(store)
    record = cloudflare.add(
        "example.com", "a.sub.example.com", "managed-by:homeproxy route:sub",
        content="home.sub.example.com", proxied=True,
    )  # fmt: skip
    before = dict(cloudflare.zone("example.com")[record])

    results = reconcile_store(store)

    assert results[2].errors == ["the API token cannot read the zone sub.example.com"]
    assert cloudflare.zone("example.com")[record] == before
    assert not [call for call in cloudflare.writes() if call.endswith(f"/{record}")]


def test_failed_route_load_deletes_nothing(cloudflare: FakeCloudflare, store: Store) -> None:
    cloudflare.add("example.com", "jellyfin.example.com", "managed-by:homeproxy route:jellyfin")
    store.route_path("jellyfin").write_text("version: 1\nname: jellyfin\ntype: [broken\n")
    snapshot(store)

    with pytest.raises(ReconcileError, match="could not be loaded"):
        reconcile_store(store)

    assert cloudflare.calls == []


@pytest.mark.parametrize("case", ["hand-edit", "hand-edit-then-allowlist", "no-snapshot"])
def test_config_that_is_not_applied_makes_no_call(
    cloudflare: FakeCloudflare, store: Store, unreachable: HAProxyClient, case: str
) -> None:
    if case == "no-snapshot":
        shutil.rmtree(store.root / "history")
    else:
        set_route(store, "jellyfin", "exposure", "lan")
    if case == "hand-edit-then-allowlist":
        add_to_allowlist(store, unreachable, "192.168.1.10")

    with pytest.raises(ReconcileError, match="not applied yet"):
        reconcile_store(store)

    assert cloudflare.calls == []


def test_a_live_allowlist_edit_keeps_the_config_applied(
    cloudflare: FakeCloudflare, store: Store, unreachable: HAProxyClient
) -> None:
    add_to_allowlist(store, unreachable, "192.168.1.10")

    assert [r.zone for r in reconcile_store(store)] == ["example.com", "example.net"]


def test_dry_run_lists_changes_without_writing(cloudflare: FakeCloudflare, store: Store) -> None:
    gone = cloudflare.add("example.net", "old.example.net", "managed-by:homeproxy route:old")
    stale = cloudflare.add(
        "example.net", "whoami.example.net", "managed-by:homeproxy route:whoami", proxied=True
    )

    results = reconcile_store(store, dry_run=True)

    assert results[1].changes == [
        Change("delete", "old.example.net", "home.example.net", False, "old", gone),
        Change("create", "git.example.net", "home.example.net", False, "git"),
        Change(
            "update", "whoami.example.net", "home.example.net", False, "whoami", stale,
            "home.example.net", True,
        ),
    ]  # fmt: skip
    assert cloudflare.writes() == []


def test_integration_off_makes_no_calls_and_purge_still_works(
    cloudflare: FakeCloudflare, store: Store
) -> None:
    reconcile_store(store)
    tree = load_tree(store.config_path)
    tree["cloudflare"]["enabled"] = False
    write_tree(store.config_path, tree)
    snapshot(store)
    cloudflare.calls.clear()

    assert reconcile_store(store) == []
    assert cloudflare.calls == []

    purge(store.load_config(), store.load_secrets())

    assert cloudflare.zone("example.com") == cloudflare.zone("example.net") == {}


MALFORMED = json.dumps(ok([{"id": "x", "name": "a.example.com"}])).encode()


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        pytest.param(None, "the API token cannot read the zone example.com", id="no-zone"),
        pytest.param(
            (200, {"Content-Length": "100"}, b'{"success"'),
            "Cloudflare sent an answer HomeProxy could not read",
            id="cut-off-answer",
        ),
        pytest.param(
            (200, {"Content-Length": str(len(MALFORMED))}, MALFORMED),
            "unexpected answer listing records", id="record-without-type",
        ),
        pytest.param(
            (429, {"Retry-After": "inf", "Content-Length": "2"}, b"{}"),
            "rate limited, retry after inf s", id="endless-rate-limit",
        ),
    ],
)  # fmt: skip
def test_failing_zone_is_reported_and_the_others_still_run(
    cloudflare: FakeCloudflare,
    store: Store,
    answer: tuple[int, dict[str, str], bytes] | None,
    error: str,
) -> None:
    if answer is None:
        del cloudflare.zones["example.com"]
    else:
        cloudflare.broken["GET /zones/id-example.com/dns_records"] = answer

    results = reconcile_store(store)

    assert len(results[0].errors) == 1
    assert error in results[0].errors[0]
    assert state(cloudflare, "example.net") == EXPECTED["example.net"]
