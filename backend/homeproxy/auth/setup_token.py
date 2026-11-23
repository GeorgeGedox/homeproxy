"""The one-time setup token.

With no `config.yaml`, the API makes a random token at start and prints it to the container log.
The setup wizard must send it back. It lives only in memory: a restart in setup mode prints a new
one and the old one stops working. It works once. The token is not a secret from `secrets.yaml`,
and printing it is the point, so it is written straight to stdout and not through `logging`.
"""

import hmac
import secrets
import sys
import threading
from typing import TextIO


class SetupToken:
    """A token that can be claimed once."""

    def __init__(self, value: str | None = None) -> None:
        """Use `value`, or make a random one with 192 bits."""
        self._value = value or secrets.token_urlsafe(24)
        self._state = "open"
        self._lock = threading.Lock()

    @property
    def value(self) -> str:
        """The token text, for the startup banner."""
        return self._value

    def _matches(self, candidate: str) -> bool:
        return hmac.compare_digest(candidate.encode(), self._value.encode())

    def claim(self, candidate: str) -> bool:
        """Reserve the token if `candidate` matches and nobody else holds it. Return the result.

        A claim ends with `finish` when setup succeeds or `release` when it fails.
        """
        with self._lock:
            if self._state != "open" or not self._matches(candidate):
                return False
            self._state = "claimed"
            return True

    def release(self) -> None:
        """Make a claimed token usable again, because setup failed."""
        with self._lock:
            if self._state == "claimed":
                self._state = "open"

    def finish(self) -> None:
        """Use the token up. It never matches again."""
        with self._lock:
            self._state = "used"

    @property
    def used(self) -> bool:
        """Whether setup finished with this token."""
        return self._state == "used"


def announce(
    token: SetupToken, url: str, fingerprint: str | None = None, out: TextIO | None = None
) -> None:
    """Print the setup banner with the token, the URL and the certificate fingerprint if known."""
    stream = out or sys.stdout
    lines = [
        "",
        "=" * 72,
        "HomeProxy is not set up yet.",
        f"Open {url}/setup from a device on your LAN and enter this setup token:",
        "",
        f"    {token.value}",
        "",
        *(
            [
                "The browser warns about the self-signed certificate. Its SHA-256 fingerprint is",
                f"    {fingerprint}",
            ]
            if fingerprint
            else []
        ),
        "The token works once. Restarting the container prints a new one.",
        "=" * 72,
        "",
    ]
    stream.write("\n".join(lines) + "\n")
    stream.flush()
