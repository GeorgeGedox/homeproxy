"""An advisory lock between writers, in one process or in several.

`flock` belongs to the open file, so a second `file_lock` on the same path waits even in the same
thread. The kernel releases the lock when the holder dies, so a crash leaves no stale lock. The lock
file stays on disk, and nothing in the product deletes it. Descriptors from `os.open` are closed on
exec, so an exec'd subprocess cannot keep the lock alive. A child made with a bare `fork()` shares
the open file and does keep it until the child exits.
"""

import fcntl
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from homeproxy.store.errors import LockNotHeld, StoreError

_POLL_INTERVAL = 0.05


class LockTimeout(StoreError):
    """Another holder kept the lock past the caller's timeout."""

    def __init__(self) -> None:
        super().__init__("another change is in progress; try again in a moment")


class HeldLock:
    """A guard against misuse: store writes check that the caller still holds the store's lock.

    It is not proof, because any code can build one. It catches a lock that was released or that
    belongs to another path.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._active = True

    def require(self, path: Path) -> None:
        """Raise `LockNotHeld` unless this is the live lock on `path`."""
        if not self._active or self._path != path:
            raise LockNotHeld(f"the lock on {path.name!r} is not held")

    def _release(self) -> None:
        self._active = False


def _acquire(descriptor: int, timeout: float | None) -> None:
    """Take the lock, polling without blocking when there is a timeout."""
    if timeout is None:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        return
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                break
        time.sleep(_POLL_INTERVAL)
    raise LockTimeout()


@contextmanager
def file_lock(path: Path, timeout: float | None = None) -> Iterator[HeldLock]:
    """Hold an exclusive lock on `path`, creating the file if needed.

    Without `timeout`, wait as long as it takes. With one, raise `LockTimeout` once that many
    seconds pass, so an API request behind a long apply answers instead of holding its thread.

    This call blocks its thread. Call it from a worker thread and never hold it across an `await`,
    because a second caller on the event loop would then wait forever for a holder that cannot run.
    Do not nest it on one path: the inner call would wait for the outer one. Pass the `HeldLock` to
    the functions that need it instead.
    """
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        _acquire(descriptor, timeout)
        held = HeldLock(path)
        try:
            yield held
        finally:
            held._release()
    finally:
        # Closing the only descriptor for this open file releases the lock.
        os.close(descriptor)
