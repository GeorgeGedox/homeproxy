"""Allowlist edits in `config.yaml` and the live release, with HAProxy out of reach.

`tests/integration/crowdsec/test_crowdsec_decisions_live.py` covers edits that reach a real HAProxy.
"""

import shutil
from pathlib import Path

import pytest

from homeproxy.crowdsec import ChangesPending, add_to_allowlist, remove_from_allowlist
from homeproxy.haproxy.client import HAProxyClient, HAProxyConnectionError
from homeproxy.store import Store, ValidationFailed
from homeproxy.store.pending import PendingChanges

SEED = Path(__file__).parents[4] / "examples" / "data"


@pytest.fixture
def store(tmp_path: Path) -> Store:
    """The example data, with a live release that holds an empty `allow.lst`."""
    shutil.copytree(SEED, tmp_path, dirs_exist_ok=True)
    (tmp_path / "releases" / "live").mkdir(parents=True)
    (tmp_path / "releases" / "live" / "allow.lst").write_bytes(b"")
    (tmp_path / "generated").symlink_to("releases/live")
    return Store(tmp_path)


@pytest.fixture
def unreachable(tmp_path: Path) -> HAProxyClient:
    """A client whose sockets do not exist, so any call to HAProxy fails."""
    missing = str(tmp_path / "missing.sock")
    return HAProxyClient(missing, missing)


def live_list(store: Store) -> bytes:
    return (store.root / "generated" / "allow.lst").read_bytes()


def test_edits_reach_config_and_the_live_file_without_haproxy_while_crowdsec_is_off(
    store: Store, unreachable: HAProxyClient
) -> None:
    """With CrowdSec off, HAProxy has no `allow.lst` ACL, so only the files change."""
    add_to_allowlist(store, unreachable, "10.0.0.1/32")
    # The same address in another spelling is not added twice.
    assert add_to_allowlist(store, unreachable, "10.0.0.1") == ["10.0.0.1/32"]
    assert add_to_allowlist(store, unreachable, "2001:db8::/32") == ["10.0.0.1/32", "2001:db8::/32"]
    assert store.load_config().crowdsec.allowlist == ["10.0.0.1/32", "2001:db8::/32"]
    assert live_list(store) == b"10.0.0.1/32\n2001:db8::/32\n"
    assert "# Seeded config" in store.config_path.read_text()  # comments stay

    assert remove_from_allowlist(store, unreachable, "10.0.0.1") == ["2001:db8::/32"]
    assert remove_from_allowlist(store, unreachable, "2001:db8::/32") == []
    assert live_list(store) == b""


def test_unreachable_haproxy_keeps_the_saved_entry_and_a_retry_is_allowed(
    store: Store, unreachable: HAProxyClient
) -> None:
    (store.root / "generated" / "crowdsec-spoe.cfg").write_bytes(b"")

    for _ in range(2):
        with pytest.raises(HAProxyConnectionError):
            add_to_allowlist(store, unreachable, "203.0.113.7")
        # The live file still matches what HAProxy runs, and the next apply renders the entry.
        assert live_list(store) == b""
        assert store.load_config().crowdsec.allowlist == ["203.0.113.7"]


def test_an_invalid_entry_changes_nothing(store: Store, unreachable: HAProxyClient) -> None:
    """`tests/unit/models/` owns the entry rules. An injection attempt stands for all of them."""
    before = store.config_path.read_bytes()

    with pytest.raises(ValidationFailed):
        add_to_allowlist(store, unreachable, "10.0.0.1\n    bind :1")

    assert store.config_path.read_bytes() == before
    assert live_list(store) == b""


def test_pending_changes_block_an_edit(store: Store, unreachable: HAProxyClient) -> None:
    """A staged `config.yaml` holds the old allowlist, so an apply would undo the edit."""
    before = store.config_path.read_bytes()
    with store.lock() as held:
        PendingChanges(store).stage("config.yaml", before + b"# edited\n", held)

    with pytest.raises(ChangesPending):
        add_to_allowlist(store, unreachable, "203.0.113.7")

    assert store.config_path.read_bytes() == before
