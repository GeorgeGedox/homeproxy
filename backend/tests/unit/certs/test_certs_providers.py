"""The checked-in provider catalog and the limits on what it lets reach lego.

`tests/integration/certs/` checks the catalog against the pinned lego.
"""

from pathlib import Path

import pytest

from homeproxy.certs.providers import (
    REFUSED_VARIABLES,
    LegoProvider,
    LegoVariable,
    check_paths,
    removed,
    variables,
)

# Names that change how any process, or lego itself, runs.
PROCESS_VARIABLES = {"PATH", "HOME", "TMPDIR", "SSL_CERT_FILE", "SSL_CERT_DIR"}


def test_no_provider_takes_a_variable_that_changes_how_lego_runs() -> None:
    """A lego bump that documents such a name for a provider must not let it through unseen."""
    names = set().union(*variables().values())

    assert names
    assert not names & (PROCESS_VARIABLES | REFUSED_VARIABLES)
    assert not [name for name in names if name.startswith(("LD_", "LEGO_"))]


@pytest.fixture
def secrets_dir(tmp_path: Path) -> Path:
    """`/data/secrets` with one key file, and a file outside it."""
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "aws.ini").write_text("[default]\n")
    (tmp_path / "outside.ini").write_text("[default]\ncredential_process = /bin/true\n")
    (tmp_path / "secrets" / "link.ini").symlink_to(tmp_path / "outside.ini")
    return tmp_path / "secrets"


def test_a_path_variable_may_name_a_file_in_the_secrets_directory(secrets_dir: Path) -> None:
    env = {"AWS_SHARED_CREDENTIALS_FILE": str(secrets_dir / "aws.ini"), "AWS_REGION": "eu-west-1"}

    check_paths(env, secrets_dir)


@pytest.mark.parametrize(
    "path",
    [
        pytest.param("{root}/outside.ini", id="outside"),
        pytest.param("{root}/secrets/../outside.ini", id="dot-dot"),
        pytest.param("{root}/secrets/link.ini", id="symlink-out"),
        pytest.param("secrets/aws.ini", id="relative"),
        pytest.param("{root}/secrets", id="the-directory-itself"),
    ],
)
def test_a_path_variable_that_leads_out_of_the_secrets_directory_is_refused(
    secrets_dir: Path, path: str
) -> None:
    """lego would read that file, and an AWS profile in it can run a program."""
    env = {"AWS_SHARED_CREDENTIALS_FILE": path.format(root=secrets_dir.parent)}

    with pytest.raises(ValueError, match="AWS_SHARED_CREDENTIALS_FILE must be the path of a file"):
        check_paths(env, secrets_dir)


def test_a_path_variable_may_not_name_secrets_yaml(secrets_dir: Path) -> None:
    """acme-dns saves its account to this path, which would overwrite every stored secret."""
    env = {"ACME_DNS_STORAGE_PATH": str(secrets_dir / "secrets.yaml")}

    with pytest.raises(ValueError, match=r"ACME_DNS_STORAGE_PATH must not be secrets\.yaml"):
        check_paths(env, secrets_dir)


def provider(code: str, *names: str) -> LegoProvider:
    found = [LegoVariable(name=name, description="") for name in names]
    return LegoProvider(code=code, name=code, credentials=found, options=[])


def test_a_lego_bump_that_removes_a_provider_or_variable_is_reported() -> None:
    old = [provider("route53", "AWS_REGION", "AWS_TTL"), provider("gone", "GONE_KEY")]
    new = [provider("route53", "AWS_REGION", "AWS_NEW"), provider("added")]

    assert removed(old, new) == ["route53 AWS_TTL", "gone"]
