"""The record table built from config and routes."""

from pathlib import Path

from homeproxy.dns.snapshot import Snapshot, build_snapshot
from homeproxy.models import parse_route
from homeproxy.store import Store

DATA = Path(__file__).parents[4] / "examples" / "data"
LAN_IP = "192.168.1.10"


def test_snapshot_has_zones_route_hosts_and_extra_records() -> None:
    store = Store(DATA)
    disabled = parse_route(
        {
            "version": 1,
            "name": "old",
            "type": "http",
            "hosts": ["old.example.com"],
            "exposure": "lan",
            "upstreams": [{"address": "192.168.1.50:80"}],
            "enabled": False,
        }
    )
    redirect = parse_route(
        {
            "version": 1,
            "name": "www",
            "type": "redirect",
            "hosts": ["www.example.com"],
            "exposure": "public",
            "redirect": {"to": "https://example.com"},
        }
    )

    snapshot = build_snapshot(store.load_config(), [*store.load_routes(), disabled, redirect])

    assert snapshot == Snapshot(
        listen="192.168.1.10:5335",
        forward_to=["1.1.1.1", "9.9.9.9"],
        zones=["example.com", "example.net"],
        records={
            "nas.example.com": "192.168.1.30",
            "files.example.net": "192.168.1.30",
            "git.example.net": LAN_IP,
            "jellyfin.example.com": LAN_IP,
            "pve.example.com": LAN_IP,
            "whoami.example.com": LAN_IP,
            "whoami.example.net": LAN_IP,
            "www.example.com": LAN_IP,
        },
    )
