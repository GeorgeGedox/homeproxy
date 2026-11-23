"""Tell the running DNS process to reread the live release."""

import logging
import os
import signal
from pathlib import Path

from homeproxy.dns.service import PID_FILE

logger = logging.getLogger(__name__)


def reload_dns(pid_file: Path = PID_FILE) -> None:
    """Send SIGHUP to the pid in `pid_file`. A DNS process that is not running is logged only,
    because it reads the live release when s6 starts it again."""
    try:
        pid = int(pid_file.read_text())
        os.kill(pid, signal.SIGHUP)
    except FileNotFoundError:
        logger.info("the DNS process is not running; it reads the live release when it starts")
    except ProcessLookupError:
        logger.warning(
            "stale DNS pid file %s; the restarted process reads the live release", pid_file
        )
    except (OSError, ValueError) as exc:
        logger.warning("cannot signal the DNS process: %s", exc)
