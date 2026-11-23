"""The lego command line. A fake `lego` records how it was called, since the real one needs a CA.

`tests/integration/certs/` runs the real binary.
"""

import itertools
import json
import sys
from pathlib import Path

import pytest

from homeproxy.certs.errors import CertificateError
from homeproxy.certs.lego import SYSTEM_PATH, Lego
from homeproxy.certs.plan import CertificateSpec
from homeproxy.models.secrets import DnsCredential
from homeproxy.util.redact import REDACTED, redact

TOKEN = "unit-test-cloudflare-token-1234"  # noqa: S105 - a fake token
CLOUDFLARE = DnsCredential.model_validate({"provider": "cloudflare", "api_token": TOKEN})
SPEC = CertificateSpec(
    "example.com", ("example.com", "*.example.com"), "example.com", "cloudflare", "cf-main"
)

# Python adds LC_CTYPE to its own environment when the locale is unset.
FAKE_LEGO = """\
import json, os, sys
env = {{k: v for k, v in os.environ.items() if k != "LC_CTYPE"}}
record = {{"argv": sys.argv[1:], "env": env}}
with open({record!r}, "w") as file:
    json.dump(record, file)
sys.stdout.write({stdout!r})  # lego 5 logs to stdout
sys.exit({status})
"""


def fake_lego(tmp_path: Path, stdout: str = "", status: int = 0) -> Lego:
    script = tmp_path / "lego"
    record = tmp_path / "record.json"
    body = FAKE_LEGO.format(record=str(record), stdout=stdout, status=status)
    script.write_text(f"#!{sys.executable}\n{body}")
    script.chmod(0o755)
    return Lego(acme_dir=tmp_path / "acme", binary=str(script), secrets_dir=tmp_path / "secrets")


def recorded(tmp_path: Path) -> dict[str, object]:
    data: dict[str, object] = json.loads((tmp_path / "record.json").read_text())
    return data


def pairs(argv: list[str]) -> list[tuple[str, str]]:
    return list(itertools.pairwise(argv))


def base_env(tmp_path: Path) -> dict[str, str]:
    """What lego gets besides the credential: a fixed PATH and its own HOME."""
    return {"PATH": SYSTEM_PATH, "HOME": str(tmp_path / "acme" / "home")}


@pytest.mark.parametrize("force", [False, True])
def test_lego_solves_dns01_against_public_resolvers_with_the_token_only_in_its_environment(
    tmp_path: Path, force: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOMEPROXY_TEST_API_ONLY", "never reaches lego")

    fake_lego(tmp_path).obtain(SPEC, CLOUDFLARE, force=force)

    call = recorded(tmp_path)
    argv = call["argv"]
    assert isinstance(argv, list)
    assert argv[argv.index("run") - 2 : argv.index("run")] == ["--log.format", "text"]
    for pair in [
        ("--dns", "cloudflare"),
        ("--dns.resolvers", "1.1.1.1:53"),
        ("--domains", "example.com"),
        ("--domains", "*.example.com"),
        ("--cert.name", "example.com"),
        ("--renew-days", "30"),
        ("--path", str(tmp_path / "acme")),
        ("--server", "https://acme-v02.api.letsencrypt.org/directory"),
    ]:
        assert pair in pairs(argv)
    assert ("--renew-force" in argv) is force
    assert not any(TOKEN in arg for arg in argv)
    tokens = {"CLOUDFLARE_DNS_API_TOKEN": TOKEN, "CLOUDFLARE_ZONE_API_TOKEN": TOKEN}
    assert call["env"] == base_env(tmp_path) | tokens


def test_another_provider_gets_exactly_its_variables_and_only_secrets_are_redacted(
    tmp_path: Path,
) -> None:
    password = "httpreq-password-a b'c"  # noqa: S105 - a fake password
    env = {
        "HTTPREQ_ENDPOINT": "http://192.168.1.10:8080",
        "HTTPREQ_PASSWORD": password,
        "HTTPREQ_POLLING_INTERVAL": "2",
    }
    credential = DnsCredential.model_validate({"provider": "httpreq", "env": env})

    fake_lego(tmp_path).obtain(SPEC, credential)

    call = recorded(tmp_path)
    assert isinstance(call["argv"], list)
    assert ("--dns", "httpreq") in pairs(call["argv"])
    assert call["env"] == base_env(tmp_path) | env
    assert redact(password) == REDACTED
    plain = "every 2 s at http://192.168.1.10:8080"
    assert redact(plain) == plain


def test_a_path_outside_the_secrets_directory_never_reaches_lego(tmp_path: Path) -> None:
    """A hand-edited `secrets.yaml` can hold any path. The API refuses it at save already."""
    env = {"AWS_SHARED_CREDENTIALS_FILE": "/etc/passwd"}
    credential = DnsCredential.model_validate({"provider": "route53", "env": env})

    with pytest.raises(CertificateError, match="AWS_SHARED_CREDENTIALS_FILE"):
        fake_lego(tmp_path).obtain(SPEC, credential)

    assert not (tmp_path / "record.json").exists()


def test_lego_files_are_named_after_the_certificate(tmp_path: Path) -> None:
    lego = Lego(acme_dir=tmp_path)

    assert lego.files("example.com") == (
        tmp_path / "certificates" / "example.com.crt",
        tmp_path / "certificates" / "example.com.key",
    )


def test_failure_shows_lego_error_with_the_token_redacted(tmp_path: Path) -> None:
    stdout = (
        "time=2026-11-10T10:00:00Z level=INFO msg=Obtaining\n"
        'time=2026-11-10T10:00:01Z level=ERROR msg=Error error="obtain certificate: '
        f'cloudflare: \\"example.com\\" rejected {TOKEN}"\n'
    )

    with pytest.raises(CertificateError) as failure:
        fake_lego(tmp_path, stdout=stdout, status=1).obtain(SPEC, CLOUDFLARE)

    message = str(failure.value)
    assert message.startswith('lego failed for example.com: obtain certificate: cloudflare: "')
    assert TOKEN not in message
    assert "level=INFO" not in message
