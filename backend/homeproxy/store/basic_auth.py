"""Write-only basic auth users in `secrets.yaml`.

A route's users can be set and removed, but nothing here returns a hash or a password. The hashes
reach HAProxy at the next apply, which renders them into the release.
"""

from collections.abc import Mapping

from passlib.hash import sha512_crypt
from pydantic import SecretStr
from ruamel.yaml.comments import CommentedMap

from homeproxy.store.errors import StoreError
from homeproxy.store.files import Store
from homeproxy.store.lock import HeldLock
from homeproxy.store.tree import load_tree
from homeproxy.util.redact import register_secret

HASH_ROUNDS = 5000
"""HAProxy runs `crypt` on every request that carries credentials, so the cost per hash stays at
the SHA-512 crypt default. At this value the hash leaves out `rounds=`."""

MIN_LENGTH = 8
"""Every password is registered for redaction, and a short one would garble HAProxy's answers."""
MAX_LENGTH = 1024


class BasicAuthError(StoreError):
    """A basic auth change cannot be made. The message names the user, never a password."""


def hash_basic_auth_password(password: str) -> SecretStr:
    """Return the SHA-512 crypt hash of a password, with a new random salt. Checks nothing."""
    register_secret(password)
    hashed: str = sha512_crypt.using(rounds=HASH_ROUNDS).hash(password)
    return SecretStr(hashed)


def _hash(user: str, password: str) -> str:
    if not MIN_LENGTH <= len(password) <= MAX_LENGTH:
        raise BasicAuthError(
            f"user {user!r}: the password must have {MIN_LENGTH} to {MAX_LENGTH} characters"
        )
    # HAProxy passes the password to `crypt` as a C string, which ends at the first NUL.
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in password):
        raise BasicAuthError(f"user {user!r}: the password must not hold control characters")
    return hash_basic_auth_password(password).get_secret_value()


def set_basic_auth(
    store: Store, route: str, passwords: Mapping[str, str | None], lock: HeldLock
) -> None:
    """Make `passwords` the users of a route in `secrets.yaml`.

    A password is hashed and replaces the user's hash. `None` keeps a user's hash, and raises
    `BasicAuthError` for a new user. A user left out is removed, so an empty mapping removes the
    route's entry. `secrets.yaml` is validated before and after the change, so a broken file or a
    bad route or user name raises `ValidationFailed` and nothing is written. Hold `lock` from before
    the call.
    """
    current = store.load_secrets().basic_auth.get(route, {})
    tree = load_tree(store.secrets_path)
    users = CommentedMap()
    for user, password in passwords.items():
        if password is not None:
            users[user] = _hash(user, password)
        elif user in current:
            users[user] = current[user].get_secret_value()
        else:
            raise BasicAuthError(f"user {user!r} is new and needs a password")
    basic_auth = tree.setdefault("basic_auth", CommentedMap())
    if users:
        basic_auth[route] = users
    else:
        basic_auth.pop(route, None)
    store.save_secrets(tree, lock)
