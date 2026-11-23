"""The pending changes in plain words: one item per change, labelled by what the user sees."""

import pytest

from homeproxy.auth.passwords import hash_password
from homeproxy.haproxy.change_summary import change_summary
from homeproxy.models import ChangeSummary

LIVE_HASH = hash_password("the live password")
TOKEN = "a-secret-header-token"  # noqa: S105
OLD_USER = "    user alice password $6$old$aaaa\n"
NEW_USER = "    user alice password $6$new$bbbb\n"


def route(hosts: str = "[app.example.com]", extra: str = "") -> bytes:
    """An http route file named `app`."""
    return (
        f"version: 1\nname: app\ntype: http\nhosts: {hosts}\nexposure: lan\n"
        f"upstreams: [{{address: 192.168.1.20:8080}}]\n{extra}"
    ).encode()


TCP = (
    b"version: 1\nname: db\ntype: tcp\nlisten_port: 5432\nexposure: lan\n"
    b"upstreams: [{address: 192.168.1.20:5432}]\n"
)


def config(extra: str = "", password_hash: str = LIVE_HASH, listen: str = "8443") -> bytes:
    """A `config.yaml` with `extra` appended."""
    return (
        f"version: 1\ndashboard:\n  listen: 192.168.1.10:{listen}\n"
        f'  password_hash: "{password_hash}"\n'
        "network: {lan_cidrs: [192.168.1.0/24], haproxy_lan_ip: 192.168.1.10}\n"
        "dns_server: {listen: 192.168.1.10:5335}\n" + extra
    ).encode()


def item(kind: str, action: str, label: str) -> ChangeSummary:
    return ChangeSummary.model_validate({"kind": kind, "action": action, "label": label})


LIVE_DOMAINS = "domains: [{zone: a.example.com}, {zone: b.example.com}, {zone: d.example.com}]\n"
STAGED_DOMAINS = (
    "domains: [{zone: b.example.com, dns_provider: self-signed}, {zone: c.example.com},"
    " {zone: d.example.com}]\n"
)
APP = "routes/app.yaml"


@pytest.mark.parametrize(
    ("live", "staged", "userlists", "expected"),
    [
        pytest.param(
            {}, {APP: route()}, None, [item("route", "added", "app.example.com")], id="route-added"
        ),
        pytest.param(
            {APP: route()},
            {APP: route("[a.example.com, b.example.com]", "path_prefix: /test\n")},
            None,
            [item("route", "changed", "a.example.com, b.example.com/test")],
            id="route-changed-hosts-and-path",
        ),
        pytest.param(
            {"routes/db.yaml": TCP},
            {},
            None,
            [item("route", "removed", "port 5432")],
            id="tcp-route-removed-labelled-from-live",
        ),
        pytest.param(
            {APP: b"name: [broken\n"},
            {},
            None,
            [item("file", "removed", APP)],
            id="unparsable-route-falls-back-to-file",
        ),
        pytest.param(
            {},
            {"includes/extra.cfg": b"# include\n"},
            None,
            [item("file", "added", "includes/extra.cfg")],
            id="include-added",
        ),
        pytest.param(
            {"config.yaml": config(LIVE_DOMAINS)},
            {"config.yaml": config(STAGED_DOMAINS)},
            None,
            [
                item("domain", "removed", "a.example.com"),
                item("domain", "changed", "b.example.com"),
                item("domain", "added", "c.example.com"),
            ],
            id="domains-by-zone",
        ),
        pytest.param(
            {"config.yaml": config()},
            {
                "config.yaml": config(
                    "prometheus: {enabled: true, listen: 192.168.1.10:8405}\n",
                    hash_password("another password"),
                    "9443",
                )
            },
            None,
            [item("settings", "changed", "prometheus")],
            id="settings-section-without-dashboard",
        ),
        pytest.param(
            {"config.yaml": config()},
            {"config.yaml": b"version: [\n"},
            None,
            [item("file", "changed", "config.yaml")],
            id="unparsable-config-falls-back-to-file",
        ),
        pytest.param(
            {APP: route(extra="auth: {type: basic}\n")},
            {APP: route(extra="auth: {type: basic}\n")},
            (
                f"userlist route-app\n{OLD_USER}userlist route-same\n{OLD_USER}",
                f"userlist route-app\n{NEW_USER}userlist route-same\n{OLD_USER}",
            ),
            [item("basic_auth", "changed", "app.example.com")],
            id="basic-auth-users-of-an-unchanged-route",
        ),
        pytest.param(
            {APP: route()},
            {APP: route(extra="auth: {type: basic}\n")},
            ("", f"userlist route-app\n{NEW_USER}"),
            [item("route", "changed", "app.example.com")],
            id="basic-auth-of-a-changed-route-is-the-route-item",
        ),
        pytest.param(
            {},
            {},
            (f"userlist route-gone\n{OLD_USER}", ""),
            [item("basic_auth", "changed", "basic auth users")],
            id="basic-auth-of-a-route-without-a-file",
        ),
    ],
)
def test_each_change_gets_one_plain_item(
    live: dict[str, bytes],
    staged: dict[str, bytes],
    userlists: tuple[str, str] | None,
    expected: list[ChangeSummary],
) -> None:
    assert change_summary(live, staged, userlists) == expected


def test_no_label_holds_a_secret() -> None:
    """A changed header token, password hash and basic auth hash name only what changed."""
    new_hash = hash_password("another password")
    other_route = route("[other.example.com]").replace(b"name: app", b"name: other")
    other = {"routes/other.yaml": other_route}
    live = {"config.yaml": config(), APP: route(extra="auth: {type: basic}\n")} | other
    staged = {
        "config.yaml": config(password_hash=new_hash),
        APP: route(extra=f"auth: {{type: basic}}\nrequest_headers: {{X-Token: {TOKEN}}}\n"),
    } | other
    userlists = (f"userlist route-other\n{OLD_USER}", f"userlist route-other\n{NEW_USER}")

    summary = change_summary(live, staged, userlists)

    assert [i.kind for i in summary] == ["route", "basic_auth"]
    labels = " ".join(i.label for i in summary)

    for secret in (TOKEN, LIVE_HASH, new_hash, "$6$", "alice"):
        assert secret not in labels
