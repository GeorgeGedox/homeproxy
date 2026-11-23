"""The CrowdSec SPOA bouncer's own config, `/data/crowdsec/spoa.yaml`, and its restart.

The release holds what HAProxy reads: the SPOE config and `allow.lst`. The bouncer's config holds
the LAPI key and the captcha keys, so it is not in a release. Its s6 service waits until the file
exists, and writes the bouncer's pid to `BOUNCER_PID_FILE`. The API runs as the same user, so it
may stop the bouncer, and s6 starts it again on the new file, or waits while there is none. A
bouncer that exits on its own leaves the pid file, and s6 waits a few seconds before the next try.
"""

import json
import logging
import os
import signal
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

from homeproxy.models.config import Crowdsec
from homeproxy.models.secrets import Secrets, SecretsCrowdsec
from homeproxy.util.atomic import atomic_write

logger = logging.getLogger(__name__)

SPOA_SOCKET = "/run/crowdsec-spoa/spoa.sock"
BOUNCER_PAGES = "/var/lib/crowdsec-haproxy-spoa-bouncer/html"
"""The ban and captcha pages that the image ships with the bouncer."""

BOUNCER_CONFIG = Path("crowdsec") / "spoa.yaml"
"""The bouncer's config, relative to the data directory."""

BOUNCER_PID_FILE = Path("/run/crowdsec-spoa/spoa.pid")

APPSEC_PORT = 7422
"""The AppSec component's default port, used on the LAPI's host when `appsec_url` is not set."""


def bouncer_config(crowdsec: Crowdsec, keys: SecretsCrowdsec) -> str:
    """Return the bouncer's config. JSON is YAML, and `json.dumps` quotes every value safely."""
    config: dict[str, object] = {
        "log_mode": "stdout",
        "log_level": "info",
        "update_frequency": "10s",
        "api_url": f"{crowdsec.lapi_url}/",
        "api_key": keys.bouncer_key.get_secret_value(),
        "listen_unix": SPOA_SOCKET,
        "prometheus": {"enabled": False},
    }
    if crowdsec.captcha_provider != "none":
        captcha = {
            "provider": crowdsec.captcha_provider,
            "site_key": keys.captcha_site_key.get_secret_value(),
            "secret_key": keys.captcha_secret_key.get_secret_value(),
            "signing_key": keys.captcha_signing_key.get_secret_value(),
            "fallback_remediation": "ban",
        }
        config["hosts"] = [{"host": "*", "captcha": captcha}]
    if crowdsec.appsec:
        # The bouncer asks AppSec with its own key. AppSec failing or slow lets the request through.
        default = f"http://{urlsplit(str(crowdsec.lapi_url)).hostname}:{APPSEC_PORT}"
        config["appsec_url"] = f"{crowdsec.appsec_url or default}/"
    return json.dumps(config, indent=2) + "\n"


def update_bouncer(
    data_dir: Path,
    crowdsec: Crowdsec,
    load_secrets: Callable[[], Secrets],
) -> None:
    """Write the bouncer's config, or remove it while CrowdSec is off, and restart the bouncer if
    the file changed. Raises `OSError` or `StoreError` when the file cannot be updated."""
    text = bouncer_config(crowdsec, load_secrets().crowdsec) if crowdsec.enabled else None
    path = data_dir / BOUNCER_CONFIG
    try:
        old: str | None = path.read_text()
    except FileNotFoundError:
        old = None
    if old == text:
        return
    if text is None:
        path.unlink()
    else:
        path.parent.mkdir(mode=0o700, exist_ok=True)
        atomic_write(path, text.encode(), mode=0o600)
    _stop_bouncer(BOUNCER_PID_FILE)


def _stop_bouncer(pid_file: Path) -> None:
    """Stop the bouncer for a new config. Without its pid file, s6 starts it again at once."""
    try:
        pid = int(pid_file.read_text())
        pid_file.unlink()
        os.kill(pid, signal.SIGTERM)
    except FileNotFoundError:
        logger.info("the CrowdSec bouncer is not running; it reads its config when it starts")
    except ProcessLookupError:
        logger.warning("stale CrowdSec bouncer pid file %s", pid_file)
    except (OSError, ValueError) as exc:
        logger.warning("cannot restart the CrowdSec bouncer: %s", exc)
