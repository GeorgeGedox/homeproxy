"""The dashboard password, stored as an argon2id hash in `config.yaml`.

There is no default password. The setup wizard or `homeproxy set-password` sets the first one.
"""

import threading

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

MIN_LENGTH = 10
"""The shortest password accepted when one is set. Login checks no length."""

MAX_LENGTH = 1024
"""The longest password accepted, so a huge request body cannot waste hashing time."""

# 64 MiB, 3 passes, 4 lanes: argon2-cffi's
# RFC 9106 low-memory profile.
_HASHER = PasswordHasher(
    time_cost=3, memory_cost=65536, parallelism=4, hash_len=32, salt_len=16, type=Type.ID
)

# Each hash needs 64 MiB. Two at a time keeps a burst of logins from many addresses inside the
# container's memory budget. Further callers wait.
_HASH_SLOTS = threading.BoundedSemaphore(2)


class PasswordRejected(ValueError):
    """A new password does not meet the rules. The message never holds the password."""


def check_new_password(password: str) -> None:
    """Raise `PasswordRejected` if `password` cannot be set as the dashboard password."""
    if len(password) < MIN_LENGTH:
        raise PasswordRejected(f"The password must have at least {MIN_LENGTH} characters.")
    if len(password) > MAX_LENGTH:
        raise PasswordRejected(f"The password must have at most {MAX_LENGTH} characters.")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in password):
        raise PasswordRejected("The password must not hold control characters.")


def hash_password(password: str) -> str:
    """Check a new password and return its argon2id hash in PHC format."""
    check_new_password(password)
    with _HASH_SLOTS:
        return _HASHER.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    """Return whether `password` matches `stored_hash`. A malformed hash never matches."""
    if not stored_hash or len(password) > MAX_LENGTH:
        return False
    with _HASH_SLOTS:
        try:
            return _HASHER.verify(stored_hash, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False
