"""Shared setup for the notification tests: the example data with one webhook target."""

import shutil
from pathlib import Path

import pytest

from homeproxy.notify import sender
from homeproxy.store import Store, load_tree, write_tree
from tests.conftest import Receiver

DATA = Path(__file__).parents[4] / "examples" / "data"


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sender, "BACKOFF", 0.01)


@pytest.fixture
def store(tmp_path: Path, receiver: Receiver) -> Store:
    """The example data with one webhook target for every event."""
    shutil.copytree(DATA, tmp_path / "data")
    store = Store(tmp_path / "data")
    tree = load_tree(store.config_path)
    tree["notifications"] = {"targets": [{"name": "hook", "type": "webhook"}]}
    write_tree(store.config_path, tree)
    with store.secrets_path.open("a") as secrets:
        secrets.write(f'notifications:\n  targets:\n    hook: {{url: "{receiver.url("/hook")}"}}\n')
    return store
