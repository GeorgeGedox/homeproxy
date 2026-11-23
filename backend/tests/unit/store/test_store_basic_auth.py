"""Write-only basic auth users: set, keep and remove users without ever returning a hash."""

import secrets
import shutil
from pathlib import Path

import pytest
from passlib.hash import sha512_crypt

from homeproxy.store import Store, ValidationFailed, load_tree
from homeproxy.store.basic_auth import BasicAuthError, set_basic_auth
from homeproxy.util.redact import REDACTED, redact

DATA = Path(__file__).parents[4] / "examples" / "data"


@pytest.fixture
def store(tmp_path: Path) -> Store:
    shutil.copytree(DATA, tmp_path / "data")
    return Store(tmp_path / "data")


def users(store: Store, route: str) -> dict[str, str]:
    return dict(load_tree(store.secrets_path)["basic_auth"][route])


def test_set_hashes_new_passwords_keeps_unchanged_users_and_removes_the_rest(
    store: Store,
) -> None:
    alice = users(store, "files")["alice"]
    password = "new " + secrets.token_hex(8)

    with store.lock() as held:
        set_basic_auth(store, "files", {"alice": None, "bob": password}, held)

    after = users(store, "files")
    assert after["alice"] == alice
    assert sha512_crypt.verify(password, after["bob"])
    # The default cost, so HAProxy's check on each request stays cheap.
    assert sha512_crypt.from_string(after["bob"]).rounds == 5000
    assert password not in store.secrets_path.read_text()
    assert redact(password) == redact(after["bob"]) == REDACTED

    with store.lock() as held:
        set_basic_auth(store, "files", {"bob": None}, held)
    assert list(users(store, "files")) == ["bob"]

    with store.lock() as held:
        set_basic_auth(store, "files", {}, held)
    assert load_tree(store.secrets_path)["basic_auth"] == {}
    assert store.load_secrets().dns_credentials


@pytest.mark.parametrize(
    "passwords",
    [{"carol": None}, {"carol": "7-chars"}, {"carol": "a" * 1025}, {"carol": "8-chars\0"}],
    ids=["new-user-without-password", "too-short", "too-long", "control-character"],
)
def test_refused_change_writes_nothing(store: Store, passwords: dict[str, str | None]) -> None:
    before = store.secrets_path.read_bytes()
    with store.lock() as held, pytest.raises(BasicAuthError, match="'carol'"):
        set_basic_auth(store, "files", passwords, held)
    assert store.secrets_path.read_bytes() == before


def test_bad_user_name_writes_nothing_and_quotes_no_password(store: Store) -> None:
    before = store.secrets_path.read_bytes()
    password = "kept-out " + secrets.token_hex(8)
    with store.lock() as held, pytest.raises(ValidationFailed) as caught:
        set_basic_auth(store, "files", {"bad user": password}, held)
    assert password not in str(caught.value)
    assert store.secrets_path.read_bytes() == before


@pytest.mark.parametrize(
    "broken", ["basic_auth:\n", "basic_auth:\n  files:\n"], ids=["all", "route"]
)
def test_malformed_secrets_file_is_refused_before_any_change(store: Store, broken: str) -> None:
    text = store.secrets_path.read_text()
    store.secrets_path.write_text(text.split("basic_auth:")[0] + broken)
    before = store.secrets_path.read_bytes()
    with store.lock() as held, pytest.raises(ValidationFailed):
        set_basic_auth(store, "files", {"alice": None}, held)
    assert store.secrets_path.read_bytes() == before
