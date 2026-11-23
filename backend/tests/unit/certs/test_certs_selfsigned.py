from pathlib import Path

from homeproxy.certs.selfsigned import self_signed_bundle, self_signed_current


def test_a_host_longer_than_a_common_name_gets_a_current_certificate(tmp_path: Path) -> None:
    """A CN holds 64 characters, and a per-host certificate takes any route host."""
    host = f"{'a' * 60}.example.com"
    path = tmp_path / "host.pem"

    path.write_bytes(self_signed_bundle((host,)))

    assert self_signed_current(path, (host,))
