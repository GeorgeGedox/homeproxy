"""Validation of the `notifications` sections of `config.yaml` and `secrets.yaml`."""

import base64
from typing import Any

import pytest
from pydantic import ValidationError

from homeproxy.models import Config, Secrets

PASSWORD_HASH = "$argon2id$v=19$m=65536,t=3,p=4$c2FsdHNhbHQ$aGFzaGhhc2hoYXNo"  # noqa: S105
SESSION_KEY = base64.b64encode(b"k" * 32).decode()
URL = "https://discord.example.com/api/webhooks/1/url-that-must-not-leak"


def config(targets: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "version": 1,
        "dashboard": {"listen": "192.168.1.10:8443", "password_hash": PASSWORD_HASH},
        "network": {"lan_cidrs": ["192.168.1.0/24"], "haproxy_lan_ip": "192.168.1.10"},
        "dns_server": {"listen": "192.168.1.10:5335"},
        "notifications": {"targets": targets},
    }


def secrets(notifications: dict[str, Any]) -> dict[str, Any]:
    return {"version": 1, "session_key": SESSION_KEY, "notifications": notifications}


def test_a_target_without_events_gets_every_event() -> None:
    loaded = Config.model_validate(config([{"name": "discord-main", "type": "discord"}]))
    assert "certificate_failed" in loaded.notifications.targets[0].events
    assert len(loaded.notifications.targets[0].events) == 9


@pytest.mark.parametrize(
    ("targets", "location"),
    [
        (
            [{"name": "a", "type": "webhook"}, {"name": "a", "type": "discord"}],
            ("notifications", "targets"),
        ),
        ([{"name": "a", "type": "slack"}], ("notifications", "targets", 0, "type")),
        (
            [{"name": "a", "type": "webhook", "events": ["route_up"]}],
            ("notifications", "targets", 0, "events", 0),
        ),
        ([{"name": "A_b", "type": "webhook"}], ("notifications", "targets", 0, "name")),
    ],
)
def test_bad_target_is_rejected(targets: list[dict[str, Any]], location: tuple[Any, ...]) -> None:
    with pytest.raises(ValidationError) as info:
        Config.model_validate(config(targets))
    assert [error["loc"] for error in info.value.errors()] == [location]


@pytest.mark.parametrize(("interval", "seconds"), [("30s", 30), ("2h", 7200)])
def test_heartbeat_interval_is_read_in_seconds(interval: str, seconds: int) -> None:
    data = config([])
    data["notifications"]["heartbeat"] = {"enabled": True, "interval": interval}
    assert Config.model_validate(data).notifications.heartbeat.seconds == seconds


@pytest.mark.parametrize("interval", ["29s", "500ms", "1w"])
def test_heartbeat_interval_under_30s_or_not_a_duration_is_rejected(interval: str) -> None:
    data = config([])
    data["notifications"]["heartbeat"] = {"interval": interval}
    with pytest.raises(ValidationError) as info:
        Config.model_validate(data)
    assert [error["loc"] for error in info.value.errors()] == [
        ("notifications", "heartbeat", "interval")
    ]


def test_urls_are_read_and_stay_masked() -> None:
    loaded = Secrets.model_validate(
        secrets({"targets": {"discord-main": {"url": URL}}, "heartbeat_url": f"{URL}/ping"})
    )
    assert loaded.notifications.target_url("discord-main") == URL
    assert loaded.notifications.target_url("other") == ""
    assert loaded.notifications.heartbeat_url.get_secret_value() == f"{URL}/ping"
    assert URL not in repr(loaded) + loaded.model_dump_json()


@pytest.mark.parametrize(
    ("notifications", "location"),
    [
        (
            {"targets": {"discord-main": {"url": ""}}},
            ("notifications", "targets", "discord-main", "url"),
        ),
        (
            {"targets": {"discord-main": {"url": "ftp://example.com/x"}}},
            ("notifications", "targets", "discord-main", "url"),
        ),
        (
            {"targets": {"discord-main": {"url": "https://a b"}}},
            ("notifications", "targets", "discord-main", "url"),
        ),
        (
            {"targets": {"discord-main": {"url": "https:///path"}}},
            ("notifications", "targets", "discord-main", "url"),
        ),
        ({"heartbeat_url": "**********"}, ("notifications", "heartbeat_url")),
        # urllib would quote the password in an error that skips redaction.
        (
            {"targets": {"hook": {"url": "http://alice:hunter2-token@example.com/hook"}}},
            ("notifications", "targets", "hook", "url"),
        ),
        ({"heartbeat_url": "https://example.com:99999/ping"}, ("notifications", "heartbeat_url")),
        ({"heartbeat_url": "https://example.com:abc/ping"}, ("notifications", "heartbeat_url")),
        ({"targets": {"Discord": {"url": URL}}}, ("notifications", "targets", "Discord", "[key]")),
    ],
)
def test_bad_url_or_target_name_is_rejected_without_its_value(
    notifications: dict[str, Any], location: tuple[str, ...]
) -> None:
    with pytest.raises(ValidationError) as info:
        Secrets.model_validate(secrets(notifications))
    assert [error["loc"] for error in info.value.errors()] == [location]
    assert "example.com" not in str(info.value)
    assert "hunter2" not in str(info.value)
