"""Auth state of one API process: setup token, login throttle, session signer and settings.

`AuthRuntime` is the one object the middleware, the auth routes and later API modules use. The
server builds it at start (`homeproxy serve`). An app served another way, such as the dev
`uvicorn --reload`, builds it on the first request that needs it.
"""

import base64
import logging
import os
import secrets
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from fastapi import FastAPI
from ruamel.yaml.comments import CommentedMap

from homeproxy.auth import errors
from homeproxy.auth.dashboard import (
    DashboardConfigError,
    DashboardSettings,
    DashboardSource,
    FileStamp,
    file_stamp,
)
from homeproxy.auth.listen import ListenChange
from homeproxy.auth.passwords import verify_password
from homeproxy.auth.ratelimit import LoginThrottle
from homeproxy.auth.sessions import Session, SessionSigner
from homeproxy.auth.setup_token import SetupToken, announce
from homeproxy.auth.sources import SourcePolicy, lan_policy, private_policy
from homeproxy.backup.token import token_matches
from homeproxy.store.errors import StoreError
from homeproxy.store.files import Store

logger = logging.getLogger(__name__)

DATA_DIR_ENV = "HOMEPROXY_DATA_DIR"
"""Overrides the data directory, `/data` by default. Tests point it at a temporary directory."""


def data_root() -> Path:
    """Return the data directory."""
    return Path(os.environ.get(DATA_DIR_ENV, "/data"))


def new_session_key() -> str:
    """Return a new `session_key` value: 32 random bytes in base64."""
    return base64.b64encode(secrets.token_bytes(32)).decode("ascii")


def ensure_session_key(store: Store) -> bool:
    """Create `secrets.yaml` with a new `session_key` if the file does not exist.

    An existing file is never changed, even when it is invalid. Return True if the file was made.
    """
    if store.secrets_path.exists():
        return False
    with store.lock() as held:
        if store.secrets_path.exists():
            return False
        tree = CommentedMap()
        tree["version"] = 1
        tree["session_key"] = new_session_key()
        store.save_secrets(tree, held)
    logger.info("created %s with a new session key", store.secrets_path)
    return True


class AuthRuntime:
    """Auth state for one data directory."""

    def __init__(self, store: Store) -> None:
        """Use `store`."""
        self.store = store
        self.dashboard = DashboardSource(store.config_path)
        self.throttle = LoginThrottle()
        self.setup_token: SetupToken | None = None if self.configured() else SetupToken()
        self._signer: SessionSigner | None = None
        self._signer_key = ""
        self._signer_stamp: FileStamp | None = None
        self._lock = threading.Lock()
        self.listen_change = ListenChange(store, self.source_policy)
        self.on_fallback = False
        """Whether the dashboard listens on the setup address because `dashboard.listen` could
        not be bound. `homeproxy serve` sets it."""

    @classmethod
    def start(cls, store: Store) -> "AuthRuntime":
        """Build the runtime and create the session key if needed."""
        try:
            ensure_session_key(store)
        except (OSError, StoreError) as error:
            # Login reports the problem. Healthz and HAProxy must keep working.
            logger.error("cannot create %s: %s", store.secrets_path, error)
        return cls(store)

    def configured(self) -> bool:
        """Whether `config.yaml` exists. Without it the dashboard is in setup mode."""
        return self.store.configured()

    # Settings and keys -----------------------------------------------------------------------

    def settings(self) -> DashboardSettings:
        """Return the dashboard settings. Raise an `AuthError` in setup mode or on a bad file."""
        try:
            current = self.dashboard.get()
        except DashboardConfigError as error:
            logger.error("dashboard settings unusable: %s", error)
            raise errors.unavailable(
                "config.yaml has an error in its dashboard settings. Check the container log."
            ) from None
        if current is None:
            raise errors.setup_required()
        return current

    def signer(self) -> SessionSigner:
        """Return the session signer for the current `session_key`.

        `secrets.yaml` is parsed again only when the file changes on disk.
        """
        stamp = file_stamp(self.store.secrets_path)
        with self._lock:
            if self._signer is not None and stamp is not None and stamp == self._signer_stamp:
                return self._signer
        try:
            key = self.store.load_secrets().session_key.get_secret_value()
        except (OSError, StoreError) as error:
            logger.error("cannot load %s: %s", self.store.secrets_path, error)
            raise errors.unavailable(
                "secrets.yaml cannot be read. Check the container log."
            ) from None
        with self._lock:
            if self._signer is None or key != self._signer_key:
                self._signer = SessionSigner(
                    base64.b64decode(key), revoked_path=self.store.revoked_sessions_path
                )
                self._signer_key = key
            self._signer_stamp = stamp
            return self._signer

    def source_policy(self) -> SourcePolicy:
        """Who may connect now: `network.lan_cidrs` after setup, private ranges before setup or
        while the dashboard settings are unusable. Costs one `stat` while the file is unchanged."""
        try:
            current = self.dashboard.get()
        except DashboardConfigError:
            current = None
        if current is None:
            return private_policy()
        return lan_policy(current.lan_cidrs)

    def is_lan(self, address: str) -> bool:
        """Whether a client address is in `network.lan_cidrs` as the file says now. Before setup,
        the private ranges count as LAN."""
        return self.source_policy().allows(address)

    # Sessions --------------------------------------------------------------------------------

    def login(self, peer: str, password: str) -> tuple[str, int]:
        """Check a password from `peer`. Return the new cookie value and its lifetime in seconds.

        Raises an `AuthError`: 429 while the client is throttled, 401 for a wrong password, 409 in
        setup mode and 503 when the files cannot be read. Blocks for the hash check, so call it
        from a worker thread.
        """
        wait = self.throttle.retry_after(peer)
        if wait is not None:
            raise errors.throttled(wait)
        current = self.settings()
        signer = self.signer()
        if not verify_password(current.password_hash, password):
            self.throttle.record_failure(peer)
            logger.warning("failed dashboard login from %s", peer)
            raise errors.AuthError(401, "invalid_password", "The password is wrong.")
        self.throttle.reset(peer)
        logger.info("dashboard login from %s", peer)
        return signer.issue(current.password_hash), current.session_ttl

    def authenticate(self, cookie: str | None) -> Session | None:
        """Return the session for a cookie value, or None if it is missing or not valid."""
        if not cookie:
            return None
        try:
            current = self.settings()
            signer = self.signer()
        except errors.AuthError:
            return None
        return signer.verify(cookie, current.password_hash, current.session_ttl)

    def backup_token_valid(self, peer: str, token: str) -> bool:
        """Whether `token` is the backup token. A wrong one counts as a failed login from `peer`.

        Raises an `AuthError` 429 while the client is throttled. Blocks on a file read, so call it
        from a worker thread.
        """
        wait = self.throttle.retry_after(peer)
        if wait is not None:
            raise errors.throttled(wait)
        try:
            valid = token_matches(self.store.load_secrets(), token)
        except (OSError, StoreError) as error:
            logger.error("cannot load %s: %s", self.store.secrets_path, error)
            raise errors.unavailable(
                "secrets.yaml cannot be read. Check the container log."
            ) from None
        if not valid:
            self.throttle.record_failure(peer)
            logger.warning("wrong backup token from %s", peer)
        return valid

    def logout(self, session: Session) -> None:
        """End a session before its cookie expires."""
        try:
            ttl = self.settings().session_ttl
            self.signer().revoke(session, ttl)
        except errors.AuthError:
            return

    # Setup -----------------------------------------------------------------------------------

    def setup_required(self) -> bool:
        """Whether setup is still open: no `config.yaml` and the token is not used up."""
        token_used = self.setup_token is not None and self.setup_token.used
        return not self.configured() and not token_used

    @contextmanager
    def setup_claim(self, peer: str, token: str, *, consume: bool = True) -> Iterator[None]:
        """Hold the setup token while the caller writes the first config.

        Checks, in order: setup is still open (409), the client is in a private range (403), the
        client is not throttled (429) and the token matches and is free (403). A wrong token counts
        as a failed login. If the block raises, the token is released for another try. If it
        returns, the token is used up, unless `consume` is false: a check then releases it and
        leaves the throttle as it is. The server keeps running on the address it is on.

        Usage in the setup endpoint: `with runtime.setup_claim(ip, body.token): ...`.
        Write the files under `Store.lock()` inside the block.
        """
        if not self.setup_required() or self.setup_token is None:
            raise errors.AuthError(409, "already_set_up", "HomeProxy is already set up.")
        if not private_policy().allows(peer):
            raise errors.AuthError(
                403, "source_not_private", "Setup is open only from private addresses."
            )
        wait = self.throttle.retry_after(peer)
        if wait is not None:
            raise errors.throttled(wait)
        if not self.setup_token.claim(token):
            self.throttle.record_failure(peer)
            logger.warning("wrong setup token from %s", peer)
            raise errors.AuthError(
                403, "invalid_setup_token", "The setup token is wrong.", field="token"
            )
        try:
            yield
        except BaseException:
            self.setup_token.release()
            raise
        if not consume:
            self.setup_token.release()
            return
        self.setup_token.finish()
        self.throttle.reset(peer)
        logger.info("setup finished from %s", peer)


_STATE_KEY = "homeproxy_auth"


def set_runtime(app: FastAPI, runtime: AuthRuntime) -> None:
    """Attach the runtime to an app. The server does this before it starts serving."""
    setattr(app.state, _STATE_KEY, runtime)


_create_lock = threading.Lock()


def get_runtime(app: FastAPI) -> AuthRuntime:
    """Return the app's runtime, building one for `data_root()` on first use."""
    runtime = getattr(app.state, _STATE_KEY, None)
    if isinstance(runtime, AuthRuntime):
        return runtime
    with _create_lock:
        runtime = getattr(app.state, _STATE_KEY, None)
        if not isinstance(runtime, AuthRuntime):
            runtime = AuthRuntime.start(Store(data_root()))
            if runtime.setup_token is not None:
                announce(runtime.setup_token, "https://<dashboard address>")
            set_runtime(app, runtime)
        return runtime
