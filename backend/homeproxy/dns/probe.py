"""Tell whether the DNS process is listening, and since when it runs, for `/status`.

The pid file only shows that the process runs: without a snapshot, or after a failed bind, it runs
without a listener. So this connects to the address the live snapshot names. TCP is bound only
together with UDP, so an accepted connection means both are up.
"""

import logging
import socket
import time
from pathlib import Path

import pydantic

from homeproxy.dns.snapshot import load_snapshot

logger = logging.getLogger(__name__)

# A stale LAN address can make the connect hang rather than be refused.
CONNECT_TIMEOUT = 1.0


def dns_listening(snapshot_path: Path) -> bool:
    """Return whether the address in the snapshot accepts a TCP connection. With no valid
    snapshot, nothing has told the DNS process where to listen, so it is not listening."""
    try:
        host, _, port = load_snapshot(snapshot_path).listen.rpartition(":")
        with socket.create_connection((host, int(port)), timeout=CONNECT_TIMEOUT):
            return True
    except (OSError, pydantic.ValidationError):
        return False


def dns_uptime_seconds(pid_file: Path) -> int | None:
    """Return the seconds since the DNS process started, from its pid file's mtime, or None when
    the file cannot be read."""
    try:
        started = pid_file.stat().st_mtime
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("cannot read the DNS pid file %s: %s", pid_file, exc)
        return None
    # The mtime is wall-clock time, so a clock step back must not show a negative uptime.
    return max(0, int(time.time() - started))
