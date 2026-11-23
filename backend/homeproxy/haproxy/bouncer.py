"""The apply pipeline's last step for CrowdSec: give the bouncer the config that is now live."""

import logging

from homeproxy.generator.crowdsec import update_bouncer
from homeproxy.models import Config
from homeproxy.store import Store, StoreError

logger = logging.getLogger(__name__)


def bouncer_warnings(store: Store, config: Config) -> list[str]:
    """Write the CrowdSec bouncer's config and return the apply warnings for a failure.

    HAProxy never waits for the bouncer, so a failure here never fails an apply.
    """
    try:
        update_bouncer(store.root, config.crowdsec, store.load_secrets)
    except (OSError, StoreError) as exc:
        logger.error("cannot update the CrowdSec bouncer config: %s", exc)
        return ["the CrowdSec bouncer config could not be updated"]
    return []
