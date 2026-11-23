"""The record table the DNS process serves: `dns-records.json` in each release.

The apply pipeline builds it from validated config with `build_snapshot` and writes it into the
release. The DNS process reads it at start and again on SIGHUP. It holds only applied state, so a
pending edit never reaches DNS answers.
"""

from collections.abc import Iterable
from pathlib import Path
from typing import Literal

from homeproxy.models import Config, Route, StrictModel, TcpRoute
from homeproxy.models.fields import Hostname, IPv4, ListenAddress

FILE_NAME = "dns-records.json"


class Snapshot(StrictModel):
    """Everything the DNS process needs: where to listen, which zones it owns, what to answer."""

    version: Literal[1] = 1
    listen: ListenAddress
    forward_to: list[IPv4]
    zones: list[Hostname]
    records: dict[Hostname, IPv4]


def build_snapshot(config: Config, routes: Iterable[Route]) -> Snapshot:
    """Return the table for `config` and its routes. Route hosts win over extra records."""
    records = {record.name: record.target for record in config.dns_records}
    for route in routes:
        # A disabled route is not served, so its name falls back to the public answer.
        if isinstance(route, TcpRoute) or not route.enabled:
            continue
        target = route.dns.target
        address = config.network.haproxy_lan_ip if target == "haproxy" else target
        records.update(dict.fromkeys(route.hosts, address))
    return Snapshot(
        listen=config.dns_server.listen,
        forward_to=config.dns_server.forward_unknown_to,
        zones=[domain.zone for domain in config.domains],
        records=records,
    )


def load_snapshot(path: Path) -> Snapshot:
    """Read and validate a snapshot file. Raises `OSError` or `pydantic.ValidationError`."""
    return Snapshot.model_validate_json(path.read_bytes())
