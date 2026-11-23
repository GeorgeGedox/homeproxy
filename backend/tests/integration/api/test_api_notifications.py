"""Notifications on the real app: failure events reach a local receiver once, in both formats,
without secrets and without delaying the apply or the job; URLs are write-only; test sends."""

import logging
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from homeproxy.api.apply import Services
from homeproxy.haproxy.pipeline import ApplyResult, Pipeline
from homeproxy.notify import sender
from homeproxy.store import Store
from tests.conftest import Receiver
from tests.integration.api.conftest import CLOUDFLARE_TOKEN

URLS = "/api/v1/notifications/urls"
TARGETS = """\
notifications:
  targets:
    - {name: discord-main, type: discord, events: [certificate_failed, apply_failed]}
    - {name: hook, type: webhook}
"""
DOMAIN_WITHOUT_CREDENTIALS = """\
domains:
  - zone: example.com
    dns_provider: cloudflare
    credentials: cf-main
"""
OUTSIDE = {
    "version": 1,
    "name": "outside",
    "type": "http",
    "hosts": ["app.example.org"],
    "exposure": "lan",
    "upstreams": [{"address": "192.168.1.20:8080"}],
}


def add_targets(root: Path, discord_url: str, hook_url: str) -> None:
    """Save both targets in `/data/config`, as an apply would, and their URLs."""
    with (root / "config" / "config.yaml").open("a") as config:
        config.write(TARGETS)
    with (root / "secrets" / "secrets.yaml").open("a") as secrets:
        secrets.write(
            f'notifications:\n  targets:\n    discord-main: {{url: "{discord_url}"}}\n'
            f'    hook: {{url: "{hook_url}"}}\n'
        )


def secret_values(root: Path) -> list[str]:
    secrets = Store(root).load_secrets()
    urls = [url.get_secret_value() for url in secrets.notifications.urls()]
    return [secrets.session_key.get_secret_value(), *filter(None, urls)]


def services(api: TestClient) -> Services:
    app: FastAPI = api.app  # type: ignore[assignment]
    found: Services = app.state.services
    return found


def fail_apply(api: TestClient) -> dict[str, Any]:
    """Stage a route outside every domain and apply it, which fails at validation."""
    assert api.post("/api/v1/routes", json=OUTSIDE).status_code == 201
    result: dict[str, Any] = api.post("/api/v1/apply").json()
    assert result["success"] is False
    return result


def fail_renewal(api: TestClient, root: Path) -> dict[str, Any]:
    """Renew a certificate whose credentials are missing, and wait for the job to fail."""
    with (root / "config" / "config.yaml").open("a") as config:
        config.write(DOMAIN_WITHOUT_CREDENTIALS)
    runner = services(api).jobs
    runner.start()
    try:
        job: dict[str, Any] = api.post("/api/v1/certificates/example.com/renew").json()
        deadline = time.monotonic() + 30
        while job["state"] not in ("finished", "failed"):
            assert time.monotonic() < deadline
            time.sleep(0.02)
            job = api.get(f"/api/v1/jobs/{job['id']}").json()
    finally:
        runner.shutdown(wait=True)
    assert job["state"] == "failed"
    return job


def test_a_failed_apply_posts_one_message_to_each_target(
    lab: Path, api: TestClient, receiver: Receiver
) -> None:
    add_targets(lab, receiver.url("/discord"), receiver.url("/hook"))
    fail_apply(api)
    Receiver.wait()

    [discord] = receiver.bodies("/discord")
    [hook] = receiver.bodies("/hook")
    error = "app.example.org is in no configured domain"
    assert discord["content"] == "HomeProxy: Apply failed at validate"
    assert discord["embeds"][0]["description"] == error
    assert (hook["event"], hook["title"], hook["message"]) == (
        "apply_failed",
        "Apply failed at validate",
        error,
    )
    for secret in [CLOUDFLARE_TOKEN, *secret_values(lab)]:
        assert all(secret not in raw for raw in receiver.raw)


def test_a_failed_renewal_posts_one_message_to_each_target(
    data_dir: Path, api: TestClient, receiver: Receiver
) -> None:
    add_targets(data_dir, receiver.url("/discord"), receiver.url("/hook"))
    job = fail_renewal(api, data_dir)
    Receiver.wait()

    [discord] = receiver.bodies("/discord")
    [hook] = receiver.bodies("/hook")
    assert discord["embeds"][0]["title"] == "Certificate example.com failed"
    assert discord["embeds"][0]["description"] == job["error"]
    assert (hook["event"], hook["message"]) == ("certificate_failed", job["error"])
    assert "cf-main" in job["error"]
    for secret in secret_values(data_dir):
        assert all(secret not in raw for raw in receiver.raw)


def test_a_rollback_posts_one_rolled_back_message(
    data_dir: Path, api: TestClient, receiver: Receiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No HAProxy runs here, so the pipeline's answer to a reload that failed verification is
    injected. The real rollback is the pipeline's own test."""
    add_targets(data_dir, receiver.url("/discord"), receiver.url("/hook"))
    rolled_back = ApplyResult(False, "verify", "reload", ("HAProxy did not verify; rolled back",))
    monkeypatch.setattr(Pipeline, "apply", lambda self, user: rolled_back)

    assert api.post("/api/v1/apply").json()["stage"] == "verify"
    Receiver.wait()

    [hook] = receiver.bodies("/hook")
    assert (hook["title"], hook["message"]) == (
        "Apply rolled back",
        "HAProxy did not verify; rolled back",
    )
    assert len(receiver.bodies("/discord")) == 1


def test_a_dead_receiver_delays_neither_an_apply_nor_a_job(
    data_dir: Path, api: TestClient, monkeypatch: pytest.MonkeyPatch, hanging_url: str
) -> None:
    monkeypatch.setattr(sender, "TIMEOUT", 2.0)
    monkeypatch.setattr(sender, "RETRIES", 0)
    add_targets(data_dir, hanging_url, hanging_url)
    started = time.monotonic()
    fail_apply(api)
    applied = time.monotonic()
    fail_renewal(api, data_dir)
    assert applied - started < sender.TIMEOUT
    assert time.monotonic() - applied < sender.TIMEOUT


def test_urls_are_written_and_cleared_but_never_returned_logged_or_audited(
    api: TestClient, data_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """URLs are saved at once in `secrets.yaml`. Reads say only which targets of the settings,
    pending ones too, have a URL, and which URLs no target uses. No URL reaches an answer, a log
    line or the audit log."""
    settings = api.get("/api/v1/settings").json()
    settings["notifications"]["targets"] = [{"name": "discord-main", "type": "discord"}]
    assert api.put("/api/v1/settings", json=settings).status_code == 200
    url = "https://discord.example.com/api/webhooks/1/url-SECRET"
    ping = "https://hc.example.com/ping-SECRET"
    none_set = {"targets": {"discord-main": False}, "unused": [], "heartbeat_url": False}
    assert api.get(URLS).json() == none_set

    with caplog.at_level(logging.DEBUG):
        written = api.put(
            URLS, json={"targets": {"discord-main": url, "old": url}, "heartbeat_url": ping}
        )
        cleared = api.put(URLS, json={"targets": {"old": ""}, "heartbeat_url": ""})

    assert written.json() == {"targets": {"discord-main": True}, "unused": ["old"]} | {
        "heartbeat_url": True
    }
    assert cleared.json() == none_set | {"targets": {"discord-main": True}}
    saved = Store(data_dir).load_secrets().notifications
    assert saved.target_url("discord-main") == url
    assert saved.targets.keys() == {"discord-main"}
    assert saved.heartbeat_url.get_secret_value() == ""
    audit = (data_dir / "audit" / "audit.jsonl").read_text()
    assert "discord-main" in audit
    shown = written.text + cleared.text + caplog.text + audit
    assert url not in shown
    assert ping not in shown


def test_the_url_of_a_live_target_is_not_left_over_while_its_rename_is_pending(
    data_dir: Path, api: TestClient
) -> None:
    """Clearing it would leave the live target with no URL once the rename is discarded."""
    add_targets(data_dir, "https://discord.example.com/1", "https://hook.example.com/1")
    settings = api.get("/api/v1/settings").json()
    settings["notifications"]["targets"][1]["name"] = "hook-renamed"
    assert api.put("/api/v1/settings", json=settings).status_code == 200

    urls = api.get(URLS).json()

    assert urls["targets"] == {"discord-main": True, "hook-renamed": False}
    assert urls["unused"] == []


def test_a_test_send_returns_the_receivers_status(
    data_dir: Path, api: TestClient, receiver: Receiver
) -> None:
    add_targets(data_dir, receiver.url("/discord"), receiver.url("/hook"))
    receiver.status = 404

    response = api.post("/api/v1/notifications/targets/hook/test")

    assert response.json() == {"status": 404, "error": None}
    assert [body["event"] for body in receiver.bodies("/hook")] == ["test"]


def test_a_test_send_to_a_receiver_that_is_not_http_answers_with_the_error(
    data_dir: Path, api: TestClient, ssh_url: str
) -> None:
    add_targets(data_dir, ssh_url, ssh_url)

    response = api.post("/api/v1/notifications/targets/hook/test")

    assert response.status_code == 200
    assert response.json()["status"] is None
    assert response.json()["error"] == "The receiver did not answer with HTTP."


def test_a_test_send_needs_a_target_with_a_url(api: TestClient) -> None:
    settings = api.get("/api/v1/settings").json()
    settings["notifications"]["targets"] = [{"name": "hook", "type": "webhook"}]
    assert api.put("/api/v1/settings", json=settings).status_code == 200

    missing = api.post("/api/v1/notifications/targets/other/test")
    unset = api.post("/api/v1/notifications/targets/hook/test")

    assert (missing.status_code, missing.json()["error"]["code"]) == (404, "target_not_found")
    assert (unset.status_code, unset.json()["error"]["code"]) == (409, "url_not_set")


def test_a_heartbeat_test_ping_needs_a_url_and_returns_the_status(
    api: TestClient, receiver: Receiver
) -> None:
    unset = api.post("/api/v1/notifications/heartbeat/test")
    api.put(URLS, json={"heartbeat_url": receiver.url("/ping")})
    receiver.status = 404

    response = api.post("/api/v1/notifications/heartbeat/test")

    assert (unset.status_code, unset.json()["error"]["code"]) == (409, "url_not_set")
    assert response.json() == {"status": 404, "error": None}
    assert receiver.pinged == ["/ping"]
