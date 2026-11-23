"""A bundle HAProxy refuses must never stay on disk, where the next start would load it.

A real HAProxy accepts any bundle that passes `build_bundle`, so a stub client injects the refusal.
"""

from pathlib import Path

import pytest

from homeproxy.certs.deploy import install_bundle
from homeproxy.certs.errors import CertificateError
from homeproxy.certs.selfsigned import self_signed_bundle
from homeproxy.haproxy.client import HAProxyClient, HAProxyCommandError, ReloadResult
from homeproxy.store.files import Store


class RefusingClient(HAProxyClient):
    """Refuses every hot swap and every reload."""

    def replace_ssl_cert(self, name: str, pem: str) -> None:
        raise HAProxyCommandError("set ssl cert failed: unable to load the certificate")

    def reload(self) -> ReloadResult:
        return ReloadResult(success=False, startup_log="[ALERT] cannot load the certificate")


@pytest.mark.parametrize("existing", [False, True], ids=["new-file", "existing-file"])
def test_refused_bundle_leaves_the_previous_file_on_disk(tmp_path: Path, existing: bool) -> None:
    target = tmp_path / "certs" / "example.com.pem"
    target.parent.mkdir()
    previous = self_signed_bundle(("example.com",))
    if existing:
        target.write_bytes(previous)

    with pytest.raises(CertificateError, match="HAProxy refused the reload"):
        install_bundle(
            "example.com",
            self_signed_bundle(("example.com",)),
            certs_dir=target.parent,
            client=RefusingClient(),
            store=Store(tmp_path),
        )

    if existing:
        assert target.read_bytes() == previous
    else:
        assert not target.exists()
    assert sorted(path.name for path in target.parent.iterdir()) == (
        ["default.pem", "example.com.pem"] if existing else ["default.pem"]
    )
