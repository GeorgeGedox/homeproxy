"""`notify`: who gets a message, in which format, with which retries, and never a secret."""

import logging
import shutil
import socket
import threading
from datetime import datetime
from pathlib import Path

import pytest

from homeproxy.notify import notify, notify_on_failure, sender
from homeproxy.store import Store
from homeproxy.util.redact import REDACTED
from tests.conftest import Receiver

DATA = Path(__file__).parents[4] / "examples" / "data"
CLOUDFLARE_TOKEN = "fake-cloudflare-token-one"  # noqa: S105 - in examples/data/secrets
TARGETS = """\
notifications:
  targets:
    - {name: discord-main, type: discord, events: [apply_failed]}
    - {name: hook, type: webhook}
"""


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sender, "BACKOFF", 0.01)
    monkeypatch.setattr(sender, "TIMEOUT", 1.0)


def make_store(tmp_path: Path, discord_url: str, hook_url: str) -> Store:
    """The example data with a Discord target for `apply_failed` and a webhook for everything."""
    shutil.copytree(DATA, tmp_path / "data")
    with (tmp_path / "data" / "config" / "config.yaml").open("a") as config:
        config.write(TARGETS)
    with (tmp_path / "data" / "secrets" / "secrets.yaml").open("a") as secrets:
        secrets.write(
            f'notifications:\n  targets:\n    discord-main: {{url: "{discord_url}"}}\n'
            f'    hook: {{url: "{hook_url}"}}\n'
        )
    return Store(tmp_path / "data")


@pytest.fixture
def store(tmp_path: Path, receiver: Receiver) -> Store:
    return make_store(tmp_path, receiver.url("/discord"), receiver.url("/hook"))


def test_each_target_gets_one_message_in_its_format(store: Store, receiver: Receiver) -> None:
    notify(store, "apply_failed", "Apply failed", "haproxy -c refused the new release")
    Receiver.wait()

    [discord] = receiver.bodies("/discord")
    [embed] = discord.pop("embeds")
    assert discord == {"content": "HomeProxy: Apply failed", "allowed_mentions": {"parse": []}}
    assert embed["title"] == "Apply failed"
    assert embed["description"] == "haproxy -c refused the new release"
    assert embed["color"] == 0xE53935
    datetime.fromisoformat(embed["timestamp"])
    [hook] = receiver.bodies("/hook")
    assert hook.pop("text") == "HomeProxy: Apply failed\nhaproxy -c refused the new release"
    datetime.fromisoformat(hook.pop("time"))
    assert hook == {
        "event": "apply_failed",
        "severity": "error",
        "title": "Apply failed",
        "message": "haproxy -c refused the new release",
    }


def test_a_target_gets_only_the_events_it_lists(store: Store, receiver: Receiver) -> None:
    notify(store, "new_version", "HAProxy 3.4.9 is available", "Update the image.")
    Receiver.wait()
    assert receiver.bodies("/discord") == []
    assert [body["severity"] for body in receiver.bodies("/hook")] == ["info"]


def test_a_secret_in_the_text_arrives_redacted(store: Store, receiver: Receiver) -> None:
    """The token and the URL are registered when the send loads `secrets.yaml`, so even a secret
    no caller registered before is redacted."""
    url = receiver.url("/hook")
    notify(store, "ddns_failed", "DDNS update failed", f"token {CLOUDFLARE_TOKEN} for {url}")
    Receiver.wait()
    [raw] = receiver.raw
    assert CLOUDFLARE_TOKEN not in raw
    assert url not in raw
    assert receiver.bodies("/hook")[0]["message"] == f"token {REDACTED} for {REDACTED}"


@pytest.mark.parametrize(
    ("statuses", "status", "posts", "logged"),
    [
        ([500, 500], 204, 3, None),
        ([], 404, 4, "HTTP 404"),
        # A webhook that moved is a failure. The redirect is never followed.
        ([], 302, 4, "HTTP 302"),
    ],
)
def test_a_failed_send_is_retried_then_logged_by_target_name(
    store: Store,
    receiver: Receiver,
    caplog: pytest.LogCaptureFixture,
    statuses: list[int],
    status: int,
    posts: int,
    logged: str | None,
) -> None:
    receiver.statuses, receiver.status = statuses, status
    with caplog.at_level(logging.WARNING):
        notify(store, "ddns_failed", "DDNS update failed", "x")
        Receiver.wait()
    assert len(receiver.bodies("/hook")) == posts
    assert [path for path, _body in receiver.received] == ["/hook"] * posts
    assert receiver.pinged == []
    if logged is None:
        assert "failed after" not in caplog.text
    else:
        assert f"ddns_failed to hook failed after 4 tries: {logged}" in caplog.text
    assert str(receiver.port) not in caplog.text


def test_a_receiver_that_is_not_http_never_stops_the_next_target(
    tmp_path: Path, receiver: Receiver, ssh_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    store = make_store(tmp_path, ssh_url, receiver.url("/hook"))
    with caplog.at_level(logging.WARNING):
        notify(store, "apply_failed", "Apply failed", "x")
        Receiver.wait()
    assert len(receiver.bodies("/hook")) == 1
    reason = "The receiver did not answer with HTTP."
    logged = f"apply_failed to discord-main failed after 4 tries: {reason}"
    assert logged in caplog.text


def test_no_answer_is_explained_in_plain_words() -> None:
    with socket.socket() as closed:
        closed.bind(("127.0.0.1", 0))
        port = closed.getsockname()[1]
    for url, reason in [
        ("http://receiver.invalid/hook", "The receiver's host name was not found."),
        (f"http://127.0.0.1:{port}/hook", "The receiver refused the connection."),
    ]:
        with pytest.raises(sender.NotifyError) as error:
            sender.send_once(url)
        assert str(error.value) == reason


def test_notify_never_waits_for_a_dead_receiver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hanging_url: str
) -> None:
    monkeypatch.setattr(sender, "TIMEOUT", 0.5)
    monkeypatch.setattr(sender, "RETRIES", 0)
    store = make_store(tmp_path, hanging_url, hanging_url)
    notify(store, "apply_failed", "Apply failed", "x")
    # The receiver never answers, so the send is still waiting when notify returns.
    assert any(thread.name == sender.THREAD_NAME for thread in threading.enumerate())


def test_a_failure_is_sent_and_raised_again(store: Store, receiver: Receiver) -> None:
    def job() -> None:
        raise OSError("disk full")

    with pytest.raises(OSError, match="disk full"):
        notify_on_failure(store, "geoip_failed", "GeoIP refresh failed", job)
    Receiver.wait()
    assert [body["message"] for body in receiver.bodies("/hook")] == ["disk full"]


def test_nothing_is_logged_before_setup(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Without `config.yaml` there are no targets yet, which is not an error."""
    with caplog.at_level(logging.WARNING):
        notify(Store(tmp_path), "new_version", "HAProxy 3.4.9 is available", "x")
        Receiver.wait()
    assert caplog.records == []
