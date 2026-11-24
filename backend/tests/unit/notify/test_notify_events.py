"""The jobs send their events: DDNS once per change of state, a new HomeProxy version once a
target got it, a failed GeoIP refresh, and each failed renewal run."""

import json
from pathlib import Path

import pytest

from homeproxy.certs.errors import CertificateError
from homeproxy.certs.lego import Lego
from homeproxy.certs.service import CertificateService
from homeproxy.geoip.country_map import GeoipError
from homeproxy.haproxy.client import HAProxyClient
from homeproxy.haproxy.pipeline import Pipeline
from homeproxy.jobs.certs import renew_due
from homeproxy.jobs.ddns import Ddns
from homeproxy.jobs.geoip import refresh
from homeproxy.jobs.update_check import UpdateCheck
from homeproxy.notify import sender
from homeproxy.store import Store, load_tree, write_tree
from tests.conftest import Receiver

NOWHERE = "http://127.0.0.1:9"
"""Nothing listens on the discard port, so a fetch from here fails at once."""


def events(receiver: Receiver) -> list[str]:
    Receiver.wait()
    return [body["event"] for body in receiver.bodies("/hook")]


def set_cloudflare(store: Store, *, enabled: bool) -> None:
    tree = load_tree(store.config_path)
    tree["cloudflare"]["enabled"] = enabled
    write_tree(store.config_path, tree)


def test_ddns_sends_one_message_each_time_it_starts_failing(
    store: Store, receiver: Receiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The state survives a restart, here a new `Ddns`. A message no target took is sent again at
    the next failure."""
    monkeypatch.setattr(sender, "RETRIES", 0)
    job = Ddns(store, NOWHERE)
    receiver.status = 500
    with pytest.raises(Exception, match="WAN address"):
        job.run()
    assert events(receiver) == ["ddns_failed"]
    receiver.status = 204
    # Runs are minutes apart, so each one's message is through before the next starts.
    for run in (job.run, job.run, Ddns(store, NOWHERE).run):
        with pytest.raises(Exception, match="WAN address"):
            run()
        Receiver.wait()
    assert events(receiver) == ["ddns_failed"] * 2

    set_cloudflare(store, enabled=False)
    job.run()
    set_cloudflare(store, enabled=True)
    with pytest.raises(Exception, match="WAN address"):
        job.run()
    assert events(receiver) == ["ddns_failed"] * 3


def test_a_new_version_is_sent_until_a_target_gets_it_and_then_never_again(
    store: Store, receiver: Receiver, tmp_path: Path
) -> None:
    """Each check is a new `UpdateCheck`, as after a restart."""
    release = tmp_path / "release.json"
    release.write_text(json.dumps({"tag_name": "v9.0.0"}))
    url = release.as_uri()
    receiver.status = 404
    UpdateCheck(url, store=store).run()
    assert len(events(receiver)) == sender.RETRIES + 1
    receiver.status = 204
    for _ in range(2):
        UpdateCheck(url, store=store).run()
        assert len(events(receiver)) == sender.RETRIES + 2
    assert receiver.bodies("/hook")[-1]["title"] == "HomeProxy 9.0.0 is available"


def test_each_failed_renewal_run_sends_one_message(store: Store, receiver: Receiver) -> None:
    """A certificate that keeps failing is reported again by each daily run."""
    tree = load_tree(store.secrets_path)
    del tree["dns_credentials"]
    write_tree(store.secrets_path, tree)
    client = HAProxyClient(str(store.root / "m.sock"), str(store.root / "a.sock"))
    service = CertificateService(store, client, Lego(store.root / "acme"), store.root / "certs")

    for runs in (1, 2):
        with pytest.raises(CertificateError):
            renew_due(service)
        assert events(receiver) == ["certificate_failed"] * runs
    assert "example.com: the credentials cf-main" in receiver.bodies("/hook")[0]["message"]


def test_a_failed_geoip_refresh_is_sent(store: Store, receiver: Receiver) -> None:
    tree = load_tree(store.config_path)
    tree["geoip"]["provider"] = "maxmind"
    write_tree(store.config_path, tree)
    pipeline = Pipeline(
        store, HAProxyClient(str(store.root / "m.sock"), str(store.root / "a.sock"))
    )

    with pytest.raises(GeoipError):
        refresh(pipeline)
    assert events(receiver) == ["geoip_failed"]
    assert receiver.bodies("/hook")[0]["message"] == "MaxMind needs a license key in secrets.yaml"
