"""Unit tests for the auth building blocks: passwords, sources, throttle, sessions, setup token."""

import io
import json
from pathlib import Path

import pytest
from itsdangerous import URLSafeTimedSerializer
from pydantic import SecretStr, TypeAdapter

from homeproxy.auth.passwords import (
    MAX_LENGTH,
    MIN_LENGTH,
    PasswordRejected,
    check_new_password,
    hash_password,
    verify_password,
)
from homeproxy.auth.ratelimit import LoginThrottle
from homeproxy.auth.sessions import SessionSigner, parse_duration
from homeproxy.auth.setup_token import SetupToken, announce
from homeproxy.auth.sources import lan_policy, private_policy
from homeproxy.models.fields import PasswordHash

GOOD = "correct horse battery"


@pytest.fixture(scope="module")
def good_hash() -> str:
    """One argon2id hash for the module, because each hash takes 64 MiB and some time."""
    return hash_password(GOOD)


class FakeClock:
    """A clock the test moves by hand."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


# Passwords ------------------------------------------------------------------------------------


def test_hash_is_argon2id_in_the_format_config_yaml_accepts(good_hash: str) -> None:
    """The hash is argon2id (64 MiB, 3 passes, 4 lanes) and passes the config validator."""
    assert good_hash.startswith("$argon2id$v=19$m=65536,t=3,p=4$")
    TypeAdapter(PasswordHash).validate_python(SecretStr(good_hash))
    assert GOOD not in good_hash


def test_verify_accepts_the_password_and_nothing_else(good_hash: str) -> None:
    """Only the exact password matches."""
    assert verify_password(good_hash, GOOD)
    assert not verify_password(good_hash, GOOD + " ")
    assert not verify_password(good_hash, "")


@pytest.mark.parametrize("stored", ["", "plain", "$argon2id$v=19$m=65536,t=3,p=4$abc$def"])
def test_malformed_hash_never_matches(stored: str) -> None:
    """A broken hash in the file fails closed."""
    assert not verify_password(stored, GOOD)


def test_overlong_password_is_refused_without_hashing(good_hash: str) -> None:
    """A huge body cannot spend hashing time."""
    assert not verify_password(good_hash, "a" * (MAX_LENGTH + 1))


@pytest.mark.parametrize(
    "password",
    ["a" * (MIN_LENGTH - 1), "a" * (MAX_LENGTH + 1), "tab\there-and-long", "newline\nhere-long"],
)
def test_new_password_rules(password: str) -> None:
    """Too short, too long and control characters are refused, and the message has no password."""
    with pytest.raises(PasswordRejected) as caught:
        check_new_password(password)
    assert password not in str(caught.value)


def test_unicode_password_is_allowed() -> None:
    """Non-ASCII passwords are fine."""
    check_new_password("pässwörd-ünïcode")


# Sources --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "peer",
    [
        "10.1.2.3",
        "172.16.0.1",
        "172.31.255.254",
        "192.168.1.20",
        "127.0.0.1",
        "169.254.10.10",
        "::1",
        "fd00::5",
        "fe80::1",
        "::ffff:192.168.1.20",
    ],
)
def test_private_policy_allows_private_ranges(peer: str) -> None:
    """Setup mode accepts RFC 1918, loopback, link-local and ULA, also IPv4-mapped."""
    assert private_policy().allows(peer)


@pytest.mark.parametrize(
    "peer",
    [
        "8.8.8.8",
        "172.32.0.1",
        "100.64.0.1",
        "198.18.0.5",
        "203.0.113.9",
        "2001:db8::1",
        "::ffff:8.8.8.8",
        "",
        None,
        "testclient",
        "192.168.1.20\n",
    ],
)
def test_private_policy_refuses_everything_else(peer: str | None) -> None:
    """Public, shared, benchmark and documentation ranges and junk are refused."""
    assert not private_policy().allows(peer)


def test_lan_policy_uses_only_the_configured_ranges() -> None:
    """After setup, a private address outside `lan_cidrs` is refused."""
    policy = lan_policy(["192.168.1.0/24", "2001:db8:1::/48"])
    assert policy.allows("192.168.1.77")
    assert policy.allows("::ffff:192.168.1.77")
    assert policy.allows("2001:db8:1::9")
    assert not policy.allows("192.168.2.1")
    assert not policy.allows("10.0.0.1")
    assert not policy.allows("127.0.0.1")


# Throttle -------------------------------------------------------------------------------------


def test_sixth_attempt_after_five_failures_is_throttled() -> None:
    """Five failures are allowed; the next attempt must wait until the window ends."""
    clock = FakeClock()
    throttle = LoginThrottle(clock=clock)
    for _ in range(5):
        assert throttle.retry_after("192.168.1.5") is None
        throttle.record_failure("192.168.1.5")
    wait = throttle.retry_after("192.168.1.5")
    assert wait == 900
    assert throttle.retry_after("192.168.1.6") is None
    clock.now += 899
    assert throttle.retry_after("192.168.1.5") == 1
    clock.now += 1
    assert throttle.retry_after("192.168.1.5") is None


def test_failures_spread_over_more_than_the_window_do_not_throttle() -> None:
    """Only failures in the last 15 minutes count."""
    clock = FakeClock()
    throttle = LoginThrottle(clock=clock)
    for _ in range(10):
        throttle.record_failure("192.168.1.5")
        clock.now += 300
    assert throttle.retry_after("192.168.1.5") is None


def test_reset_forgets_failures() -> None:
    """A successful login clears the count."""
    throttle = LoginThrottle(clock=FakeClock())
    for _ in range(4):
        throttle.record_failure("192.168.1.5")
    throttle.reset("192.168.1.5")
    for _ in range(4):
        throttle.record_failure("192.168.1.5")
    assert throttle.retry_after("192.168.1.5") is None


# Sessions -------------------------------------------------------------------------------------

KEY = bytes(range(32))


def test_session_round_trip_and_expiry() -> None:
    """A fresh cookie verifies; after the TTL it does not."""
    signer = SessionSigner(KEY)
    cookie = signer.issue("hash-a")
    assert signer.verify(cookie, "hash-a", ttl=60) is not None
    assert signer.verify(cookie, "hash-a", ttl=-1) is None


def test_session_is_bound_to_the_password_hash() -> None:
    """A password change signs every session out."""
    signer = SessionSigner(KEY)
    cookie = signer.issue("hash-a")
    assert signer.verify(cookie, "hash-b", ttl=60) is None


def test_session_from_another_key_or_tampered_is_refused() -> None:
    """Only cookies signed with this key verify."""
    cookie = SessionSigner(bytes(32)).issue("hash-a")
    signer = SessionSigner(KEY)
    assert signer.verify(cookie, "hash-a", ttl=60) is None
    good = signer.issue("hash-a")
    tampered = good[:-2] + ("AA" if not good.endswith("AA") else "BB")
    assert signer.verify(tampered, "hash-a", 60) is None
    assert signer.verify("", "hash-a", 60) is None
    assert signer.verify("garbage", "hash-a", 60) is None


def test_cookie_holds_no_password_hash() -> None:
    """The cookie is signed, not encrypted, so it must not hold the hash."""
    password_hash = "$argon2id$v=19$m=65536,t=3,p=4$c2FsdHNhbHQ$aGFzaGhhc2g"  # noqa: S105
    cookie = SessionSigner(KEY).issue(password_hash)
    # The payload is base64 JSON, so it is decoded first. Any key reads it: only the check fails.
    _, payload = URLSafeTimedSerializer(b"any key").loads_unsafe(cookie)
    assert "aGFzaGhhc2g" not in json.dumps(payload)
    assert "argon2" not in json.dumps(payload)


def test_revoked_session_is_refused_also_after_a_restart(tmp_path: Path) -> None:
    """Logout ends the session even though the cookie is still genuine, and a new process with
    the same key still refuses it."""
    path = tmp_path / "state" / "revoked-sessions.json"
    signer = SessionSigner(KEY, revoked_path=path)
    cookie = signer.issue("hash-a")
    session = signer.verify(cookie, "hash-a", 60)
    assert session is not None
    signer.revoke(session, 60)
    assert signer.verify(cookie, "hash-a", 60) is None
    assert signer.verify(signer.issue("hash-a"), "hash-a", 60) is not None
    assert SessionSigner(KEY, revoked_path=path).verify(cookie, "hash-a", 60) is None


def test_short_key_is_refused() -> None:
    """The signing key must have at least 32 bytes."""
    with pytest.raises(ValueError, match="32 bytes"):
        SessionSigner(b"short")


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("12h", 43200), ("30m", 1800), ("45s", 45), ("2d", 172800), ("1500ms", 1), ("10ms", 1)],
)
def test_parse_duration(text: str, seconds: int) -> None:
    """HAProxy durations become whole seconds."""
    assert parse_duration(text) == seconds


@pytest.mark.parametrize("text", ["", "0h", "12", "h", "1w", "-1h", "12h\n"])
def test_parse_duration_rejects_bad_input(text: str) -> None:
    """Anything that is not a positive HAProxy duration fails."""
    with pytest.raises(ValueError, match="duration"):
        parse_duration(text)


# Setup token ----------------------------------------------------------------------------------


def test_setup_token_is_random_and_long() -> None:
    """Tokens differ and carry at least 128 bits."""
    tokens = {SetupToken().value for _ in range(20)}
    assert len(tokens) == 20
    assert all(len(token) >= 32 for token in tokens)


def test_setup_token_claim_release_finish() -> None:
    """A claim holds the token; release frees it; finish uses it up for good."""
    token = SetupToken("abc-token")
    assert not token.claim("wrong")
    assert token.claim("abc-token")
    assert not token.claim("abc-token")
    token.release()
    assert token.claim("abc-token")
    token.finish()
    assert token.used
    token.release()
    assert not token.claim("abc-token")


def test_announce_prints_token_url_and_fingerprint() -> None:
    """The banner names the token, the setup URL and the fingerprint."""
    out = io.StringIO()
    announce(SetupToken("tok-123"), "https://192.168.1.10:8443", "AA:BB", out)
    text = out.getvalue()
    assert "tok-123" in text
    assert "https://192.168.1.10:8443/setup" in text
    assert "AA:BB" in text
