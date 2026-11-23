"""Dashboard sessions in signed cookies.

The cookie holds a random session id and a fingerprint of the current password hash, signed with
`session_key` from `secrets.yaml`. It is signed, not encrypted, so it holds nothing secret. It
expires after `dashboard.session_ttl`. Changing the password changes the fingerprint, so every
older session stops working. Logout adds the session id to a list kept until the cookie would
expire anyway. The list is saved to a file, so a restart does not bring a logged-out cookie back.
"""

import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from itsdangerous import BadSignature, URLSafeTimedSerializer
from pydantic import TypeAdapter, ValidationError

from homeproxy.models.fields import duration_ms
from homeproxy.util.atomic import atomic_write, ensure_dir

logger = logging.getLogger(__name__)

COOKIE_NAME = "__Host-homeproxy_session"
"""The `__Host-` prefix makes browsers require `Secure`, `Path=/` and no `Domain`."""

_SALT = "homeproxy.dashboard.session"

_REVOKED: TypeAdapter[dict[str, float]] = TypeAdapter(dict[str, float])


def parse_duration(text: str) -> int:
    """Return a duration in HAProxy's format, such as `12h`, in whole seconds (at least 1)."""
    return max(1, duration_ms(text) // 1000)


@dataclass(frozen=True)
class Session:
    """A valid session."""

    session_id: str
    issued_at: float


class SessionSigner:
    """Issues and checks session cookies with one signing key."""

    def __init__(
        self,
        key: bytes,
        clock: Callable[[], float] = time.time,
        revoked_path: Path | None = None,
    ) -> None:
        """Sign with `key`, which must be at least 32 random bytes. Logouts are saved to
        `revoked_path`, if given."""
        if len(key) < 32:
            raise ValueError("the session key must be at least 32 bytes")
        self._key = key
        self._clock = clock
        self._serializer = URLSafeTimedSerializer(
            key, salt=_SALT, signer_kwargs={"digest_method": hashlib.sha256}
        )
        self._revoked_path = revoked_path
        self._revoked = self._read_revoked()
        self._lock = threading.Lock()

    def _read_revoked(self) -> dict[str, float]:
        if self._revoked_path is None:
            return {}
        try:
            return _REVOKED.validate_json(self._revoked_path.read_bytes())
        except FileNotFoundError:
            return {}
        except (OSError, ValidationError) as error:
            logger.error("cannot read %s, earlier logouts are lost: %s", self._revoked_path, error)
            return {}

    def _save_revoked(self) -> None:
        if self._revoked_path is None:
            return
        try:
            ensure_dir(self._revoked_path.parent)
            atomic_write(self._revoked_path, json.dumps(self._revoked).encode())
        except OSError as error:
            logger.error("cannot save %s, logout ends at a restart: %s", self._revoked_path, error)

    def _fingerprint(self, password_hash: str) -> str:
        digest = hmac.new(self._key, password_hash.encode(), hashlib.sha256)
        return digest.hexdigest()[:32]

    def issue(self, password_hash: str) -> str:
        """Return a new cookie value bound to the current password hash."""
        payload = {"sid": secrets.token_urlsafe(16), "pw": self._fingerprint(password_hash)}
        return self._serializer.dumps(payload)

    def _load(self, cookie: str, ttl: int) -> tuple[dict[str, object], float] | None:
        try:
            payload, signed_at = self._serializer.loads(cookie, max_age=ttl, return_timestamp=True)
        except BadSignature:
            return None
        if not isinstance(payload, dict):
            return None
        issued = signed_at.timestamp()
        # itsdangerous accepts a timestamp from the future. Only a wrong clock makes one.
        if issued > self._clock() + 60:
            return None
        return payload, issued

    def verify(self, cookie: str, password_hash: str, ttl: int) -> Session | None:
        """Return the session if the cookie is genuine, not expired, not revoked, and was issued
        for the current password hash. Return None otherwise."""
        loaded = self._load(cookie, ttl)
        if loaded is None:
            return None
        payload, issued = loaded
        session_id, bound = payload.get("sid"), payload.get("pw")
        if not isinstance(session_id, str) or not isinstance(bound, str):
            return None
        if not hmac.compare_digest(bound, self._fingerprint(password_hash)):
            return None
        with self._lock:
            if session_id in self._revoked:
                return None
        return Session(session_id=session_id, issued_at=issued)

    def revoke(self, session: Session, ttl: int) -> None:
        """Refuse this session from now on, until its cookie would have expired."""
        now = self._clock()
        with self._lock:
            self._revoked = {sid: until for sid, until in self._revoked.items() if until > now}
            self._revoked[session.session_id] = session.issued_at + ttl
            self._save_revoked()
