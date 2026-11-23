import datetime
import itertools
import json
import shutil
import sys
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat
from cryptography.x509.oid import NameOID

from homeproxy.certs.bundle import build_bundle
from homeproxy.certs.deploy import Delivery
from homeproxy.certs.errors import CertificateError
from homeproxy.certs.lego import LETSENCRYPT, LETSENCRYPT_STAGING, RENEW_DAYS, Lego
from homeproxy.certs.plan import CertificateSpec
from homeproxy.certs.selfsigned import self_signed_bundle
from homeproxy.certs.service import DEV_SELF_SIGNED_ENV, CertificateService
from homeproxy.haproxy.client import HAProxyClient, ReloadResult
from homeproxy.store import Store, load_tree, write_tree

DATA = Path(__file__).parents[4] / "examples" / "data"
# Records its arguments and writes no certificate, like a lego that the CA refused.
FAKE_LEGO = "import json, sys\njson.dump(sys.argv[1:], open({record!r}, 'w'))\n"


def test_credentials_of_another_provider_never_reach_lego(tmp_path: Path) -> None:
    """A hand edit moves example.net to httpreq but keeps its Cloudflare credentials."""
    shutil.copytree(DATA, tmp_path / "data")
    store = Store(tmp_path / "data")
    tree = load_tree(store.config_path)
    tree["domains"][1]["dns_provider"] = "httpreq"
    write_tree(store.config_path, tree)
    lego = Lego(tmp_path / "acme", binary=str(tmp_path / "no-lego"))
    service = CertificateService(store, HAProxyClient(), lego, tmp_path / "certs")

    with pytest.raises(CertificateError, match=r"are for cloudflare, but example\.net uses"):
        service.renew("example.net")


@pytest.mark.parametrize(
    ("environ", "server", "acme"),
    [
        pytest.param({}, LETSENCRYPT, "acme", id="production"),
        pytest.param({DEV_SELF_SIGNED_ENV: "0"}, LETSENCRYPT_STAGING, "acme/staging", id="dev-0"),
        pytest.param({DEV_SELF_SIGNED_ENV: "1"}, None, None, id="dev-1"),
    ],
)
def test_dev_stack_never_reaches_production_lets_encrypt(
    tmp_path: Path, environ: dict[str, str], server: str | None, acme: str | None
) -> None:
    """The dev variable overrides `certificates.staging`. Only production can switch, and only to
    staging, which has a lego directory of its own."""
    service = CertificateService.from_environ(tmp_path, HAProxyClient(), environ)

    assert (service.lego and service.lego.server) == server
    assert (service.lego and service.lego.acme_dir) == (acme and tmp_path / acme)
    assert service.certs_dir == tmp_path / "certs"
    if service.lego is not None:
        assert service.lego.secrets_dir == tmp_path / "secrets"
    switch = (service.staging.server, service.staging.acme_dir) if service.staging else None
    assert switch == ((LETSENCRYPT_STAGING, tmp_path / "acme/staging") if not environ else None)


def lego_files(lego: Lego, spec: CertificateSpec) -> bytes:
    """Write a certificate as lego keeps it, and return its bundle."""
    bundle = self_signed_bundle(spec.domains)
    chain = x509.load_pem_x509_certificates(bundle)[0].public_bytes(Encoding.PEM)
    crt, key = lego.files(spec.name)
    crt.parent.mkdir(parents=True, exist_ok=True)
    crt.write_bytes(chain)
    key.write_bytes(bundle[len(chain) :])
    return build_bundle(chain, bundle[len(chain) :])


def set_staging(store: Store, staging: bool) -> None:
    tree = load_tree(store.config_path)
    tree["certificates"] = {"staging": staging}
    write_tree(store.config_path, tree)


def test_a_ca_switch_issues_every_certificate_from_the_chosen_ca(tmp_path: Path) -> None:
    """With `certificates.staging` on, a production bundle needs issuing again, and lego runs
    against staging in its own directory. Back on production, the staging bundle needs it too."""
    shutil.copytree(DATA, tmp_path / "data")
    store = Store(tmp_path / "data")
    script = tmp_path / "lego"
    script.write_text(f"#!{sys.executable}\n{FAKE_LEGO.format(record=str(tmp_path / 'argv'))}")
    script.chmod(0o755)
    acme = tmp_path / "data" / "acme"
    production = Lego(acme, binary=str(script))
    staging = Lego(acme / "staging", server=LETSENCRYPT_STAGING, binary=str(script))
    service = CertificateService(store, HAProxyClient(), production, tmp_path / "certs", staging)
    spec = next(s for s in service.plan() if s.name == "example.com")
    service.certs_dir.mkdir()
    installed = service.certs_dir / "example.com.pem"
    installed.write_bytes(lego_files(production, spec))
    assert not service.needs_issue(spec)

    set_staging(store, True)

    assert service.needs_issue(spec)
    with pytest.raises(CertificateError, match="lego wrote no certificate files"):
        service.renew("example.com")
    argv = json.loads((tmp_path / "argv").read_text())
    assert ("--server", LETSENCRYPT_STAGING) in itertools.pairwise(argv)
    assert ("--path", str(acme / "staging")) in itertools.pairwise(argv)

    installed.write_bytes(lego_files(staging, spec))
    assert not service.needs_issue(spec)
    set_staging(store, False)
    assert service.needs_issue(spec)


class ReloadingClient(HAProxyClient):
    """Accepts every hot swap and reload, because a unit test runs no HAProxy."""

    def replace_ssl_cert(self, name: str, pem: str) -> None:
        pass

    def reload(self) -> ReloadResult:
        return ReloadResult(success=True, startup_log="")


def stale_bundle(domains: tuple[str, ...], issuer: str | None = None, days: int = 90) -> bytes:
    """Return a bundle for `domains`, issued by `issuer` (itself if None), with `days` left."""
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.datetime.now(datetime.UTC)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, domains[0])])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(
            x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, issuer)]) if issuer else subject
        )
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=days))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(d) for d in domains]), False)
        .sign(key, hashes.SHA256())
    )
    pem_key = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    return certificate.public_bytes(Encoding.PEM) + pem_key


@pytest.mark.parametrize(
    ("names", "issuer", "days"),
    [
        pytest.param(("example.net",), None, 90, id="names-changed"),
        pytest.param(("example.net", "*.example.net"), None, RENEW_DAYS, id="due-for-renewal"),
        pytest.param(("example.net", "*.example.net"), "R11", 90, id="issued-by-lego-before"),
    ],
)
def test_a_self_signed_domain_gets_its_certificate_without_lego(
    tmp_path: Path, names: tuple[str, ...], issuer: str | None, days: int
) -> None:
    """example.net is self-signed, and staging is on, with no lego binary for either CA. Its
    certificate is made at issue and kept while current, and a stale one is made again."""
    shutil.copytree(DATA, tmp_path / "data")
    store = Store(tmp_path / "data")
    tree = load_tree(store.config_path)
    tree["domains"][1]["dns_provider"] = "self-signed"
    del tree["domains"][1]["credentials"]
    write_tree(store.config_path, tree)
    set_staging(store, True)
    no_lego = str(tmp_path / "no-lego")
    production, staging = (Lego(tmp_path / name, binary=no_lego) for name in ("acme", "staging"))
    certs = tmp_path / "certs"
    service = CertificateService(store, ReloadingClient(), production, certs, staging)
    certs.mkdir()
    spec = next(s for s in service.plan() if s.name == "example.net")
    assert spec.domains == ("example.net", "*.example.net")

    assert service.needs_issue(spec)
    assert service.renew("example.net") is Delivery.RELOADED
    assert not service.needs_issue(spec)
    assert service.renew("example.net") is Delivery.UNCHANGED

    (certs / "example.net.pem").write_bytes(stale_bundle(names, issuer, days))
    assert service.needs_issue(spec)
    assert service.renew("example.net") is Delivery.HOT_SWAPPED
    assert not service.needs_issue(spec)


def test_the_daily_renewal_is_quiet_before_setup(tmp_path: Path) -> None:
    """Without `config.yaml` there is no certificate to renew, only the default one to keep."""
    service = CertificateService(Store(tmp_path), HAProxyClient(), None, tmp_path / "certs")

    service.renew_all()

    assert (tmp_path / "certs" / "default.pem").exists()
