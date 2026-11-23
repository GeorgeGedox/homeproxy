"""Login rate limit: 5 failures per client address in 15 minutes, then a wait.

While an address is throttled, every attempt is refused before the password is checked, so even
the right password does not get through until the oldest failure leaves the window. State is in
memory and is lost on restart.
"""

import math
import threading
import time
from collections import deque
from collections.abc import Callable

from homeproxy.auth.sources import parse_address

MAX_FAILURES = 5
WINDOW_SECONDS = 15 * 60.0


def _client_key(peer: str) -> str:
    """Return the key failures are counted under: the address, as the dashboard binds IPv4 only."""
    address = parse_address(peer)
    return peer if address is None else str(address)


class LoginThrottle:
    """Counts failed attempts per client and says when a client must wait."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        """Allow `MAX_FAILURES` failures in `WINDOW_SECONDS` per client."""
        self._max = MAX_FAILURES
        self._window = WINDOW_SECONDS
        self._clock = clock
        self._failures: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> deque[float] | None:
        times = self._failures.get(key)
        if times is None:
            return None
        while times and times[0] <= now - self._window:
            times.popleft()
        if not times:
            del self._failures[key]
            return None
        return times

    def retry_after(self, peer: str) -> int | None:
        """Return the seconds a throttled client must wait, or None if it may try now."""
        now = self._clock()
        with self._lock:
            times = self._prune(_client_key(peer), now)
            if times is None or len(times) < self._max:
                return None
            return max(1, math.ceil(times[0] + self._window - now))

    def record_failure(self, peer: str) -> None:
        """Count one failed attempt."""
        now = self._clock()
        key = _client_key(peer)
        with self._lock:
            times = self._prune(key, now)
            if times is None:
                times = self._failures.setdefault(key, deque())
            times.append(now)
            # Only the newest failures matter for the wait.
            while len(times) > self._max:
                times.popleft()

    def reset(self, peer: str) -> None:
        """Forget the failures of a client, after it logs in."""
        with self._lock:
            self._failures.pop(_client_key(peer), None)
