"""The backup token: 32 random bytes, shown once, kept as a SHA-256 hash in `secrets.yaml`."""

import hashlib
import hmac
import secrets
from datetime import UTC, datetime

from homeproxy.models import Secrets

TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def new_token() -> str:
    """Return a new token, URL-safe."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """Return the hash that `secrets.yaml` keeps. A random 256-bit token needs no salt."""
    return hashlib.sha256(token.encode()).hexdigest()


def created_now() -> str:
    """Return the creation time to store with a new token."""
    return datetime.now(UTC).strftime(TIME_FORMAT)


def token_matches(stored: Secrets, token: str) -> bool:
    """Return whether `token` is the backup token in `stored`. False when none is set."""
    if stored.backup_token_hash is None:
        return False
    expected = stored.backup_token_hash.get_secret_value()
    return hmac.compare_digest(hash_token(token), expected)
