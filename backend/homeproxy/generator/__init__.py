"""Generate HAProxy release directories from validated config and routes.

`render` builds the files in memory, and `write_release` writes them to a new directory under
`/data/releases/`. `ensure_default_certificate` must have run before `haproxy -c` reads the result.
"""

from homeproxy.generator.certificate import DEFAULT_CERTIFICATE_NAME, ensure_default_certificate
from homeproxy.generator.escape import GeneratorError
from homeproxy.generator.render import (
    CERTS_DIR,
    SERVER_STATE_FILE,
    USERLISTS_CONFIG,
    BasicAuth,
    RenderedRelease,
    render,
    write_release,
)
from homeproxy.generator.route_types import BACKEND_PREFIX, RouteError

__all__ = [
    "BACKEND_PREFIX",
    "CERTS_DIR",
    "DEFAULT_CERTIFICATE_NAME",
    "SERVER_STATE_FILE",
    "USERLISTS_CONFIG",
    "BasicAuth",
    "GeneratorError",
    "RenderedRelease",
    "RouteError",
    "ensure_default_certificate",
    "render",
    "write_release",
]
