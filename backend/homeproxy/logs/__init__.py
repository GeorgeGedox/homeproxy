"""HAProxy access logs: the syslog collector and the parser for both log formats.

The generated config sends every line to `SYSLOG_SOCKET`, and to CrowdSec when it is enabled.
`LogCollector` runs in the API process, parses each line once into an `AccessLog`, keeps the newest
lines for the live tail and passes each line to its listeners.
"""

from homeproxy.logs.collector import (
    SYSLOG_SOCKET,
    TAIL_LENGTH,
    Listener,
    LogCollector,
)
from homeproxy.logs.parser import (
    HTTP_LOG_FORMAT,
    TCP_LOG_FORMAT,
    AccessLog,
    parse_message,
)

__all__ = [
    "HTTP_LOG_FORMAT",
    "SYSLOG_SOCKET",
    "TAIL_LENGTH",
    "TCP_LOG_FORMAT",
    "AccessLog",
    "Listener",
    "LogCollector",
    "parse_message",
]
