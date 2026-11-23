"""A second writer waits for the lock, in other threads and in other processes."""

import os
import signal
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

from homeproxy.store import LockNotHeld, Store, load_tree
from tests.conftest import wait_until

DATA = Path(__file__).parents[4] / "examples" / "data"
DEADLINE = 60.0


def blocked_on(lock_path: Path) -> bool:
    """Whether a writer waits for `lock_path`: Linux lists it in `/proc/locks` behind `->`. Only
    the inode is matched: on btrfs and some overlays the device there differs from `st_dev`."""
    key = f":{lock_path.stat().st_ino} "
    lines = Path("/proc/locks").read_text().splitlines()
    return any(" -> " in line and key in line for line in lines)


def spawn(code: str, *arguments: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen(  # noqa: S603 - fixed arguments in a test
        [sys.executable, "-c", textwrap.dedent(code), *arguments],
        stdin=subprocess.PIPE,
    )


HOLD = """
    import sys
    from pathlib import Path
    from homeproxy.store import Store
    root, ready = sys.argv[1:]
    with Store(Path(root)).lock():
        Path(ready).touch()
        sys.stdin.read()
"""

ACQUIRE = """
    import sys
    from pathlib import Path
    from homeproxy.store import Store
    root, acquired = sys.argv[1:]
    with Store(Path(root)).lock():
        Path(acquired).touch()
"""


def test_second_process_waits_until_the_first_releases(tmp_path: Path) -> None:
    root = tmp_path / "data"
    ready, acquired = tmp_path / "ready", tmp_path / "acquired"

    holder = spawn(HOLD, str(root), str(ready))
    wait_until(ready.exists, "the holder to take the lock", timeout=DEADLINE, interval=0.01)
    waiter = spawn(ACQUIRE, str(root), str(acquired))
    wait_until(
        lambda: blocked_on(Store(root).lock_path) or acquired.exists(),
        "the second writer to wait",
        timeout=DEADLINE,
        interval=0.01,
    )
    assert not acquired.exists()

    holder.communicate(b"")
    wait_until(
        acquired.exists, "the second writer to take the lock", timeout=DEADLINE, interval=0.01
    )
    assert waiter.wait(DEADLINE) == 0


def test_lock_is_released_when_the_holder_is_killed(tmp_path: Path) -> None:
    root = tmp_path / "data"
    ready, acquired = tmp_path / "ready", tmp_path / "acquired"

    holder = spawn(HOLD, str(root), str(ready))
    wait_until(ready.exists, "the holder to take the lock", timeout=DEADLINE, interval=0.01)
    waiter = spawn(ACQUIRE, str(root), str(acquired))
    wait_until(
        lambda: blocked_on(Store(root).lock_path),
        "the second writer to wait",
        timeout=DEADLINE,
        interval=0.01,
    )
    os.kill(holder.pid, signal.SIGKILL)
    holder.wait(DEADLINE)
    assert holder.stdin is not None
    holder.stdin.close()

    wait_until(
        acquired.exists, "the second writer to take the lock", timeout=DEADLINE, interval=0.01
    )
    assert waiter.wait(DEADLINE) == 0


def acquires_within_deadline(store: Store) -> bool:
    """Try the lock from a daemon thread, so a bug fails the test and does not hang the run."""
    done = threading.Event()

    def take() -> None:
        with store.lock():
            done.set()

    threading.Thread(target=take, daemon=True).start()
    return done.wait(DEADLINE)


def test_second_thread_waits_until_the_first_releases(tmp_path: Path) -> None:
    store = Store(tmp_path)
    done = threading.Event()

    def take() -> None:
        with store.lock():
            done.set()

    with store.lock():
        threading.Thread(target=take, daemon=True).start()
        wait_until(
            lambda: blocked_on(store.lock_path) or done.is_set(),
            "the second thread to wait",
            timeout=DEADLINE,
            interval=0.01,
        )
        assert not done.is_set()
    assert done.wait(DEADLINE)


def test_subprocess_started_under_the_lock_does_not_keep_it(tmp_path: Path) -> None:
    store = Store(tmp_path)
    with store.lock():
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        assert acquires_within_deadline(store)
    finally:
        child.kill()
        child.wait(DEADLINE)


def test_lock_is_released_after_an_exception(tmp_path: Path) -> None:
    store = Store(tmp_path)
    with pytest.raises(RuntimeError), store.lock():
        raise RuntimeError("failed under the lock")
    assert acquires_within_deadline(store)


def test_lock_file_stays_and_is_private(tmp_path: Path) -> None:
    store = Store(tmp_path / "new")
    with store.lock():
        pass
    assert store.lock_path.stat().st_mode & 0o777 == 0o600


def test_lock_file_is_outside_the_config_directory(tmp_path: Path) -> None:
    store = Store(tmp_path)
    assert store.config_dir not in store.lock_path.parents


def seed(root: Path) -> Store:
    (root / "config" / "routes").mkdir(parents=True)
    (root / "secrets").mkdir()
    (root / "config" / "config.yaml").write_bytes((DATA / "config" / "config.yaml").read_bytes())
    return Store(root)


def test_save_with_a_released_lock_is_refused(tmp_path: Path) -> None:
    store = seed(tmp_path)
    tree = load_tree(store.config_path)
    with store.lock() as held:
        pass
    before = store.config_path.read_bytes()
    tree["version"] = 1
    with pytest.raises(LockNotHeld):
        store.save_config(tree, held)
    assert store.config_path.read_bytes() == before


def test_save_with_another_stores_lock_is_refused(tmp_path: Path) -> None:
    store = seed(tmp_path / "one")
    other = Store(tmp_path / "two")
    tree = load_tree(store.config_path)
    with other.lock() as held, pytest.raises(LockNotHeld):
        store.save_config(tree, held)
