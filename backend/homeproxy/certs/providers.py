"""The DNS providers of the pinned lego, and the variables lego documents for each one.

The catalog is `lego_providers.json`, made from `lego dnshelp` by `mise run lego-providers` and
checked in. It is a file and not read from the binary at startup, so the models validate where no
lego binary exists (unit tests, a local backend), and a lego bump shows as a diff to review. An
integration test fails when the file no longer matches the lego pinned in `image/Dockerfile`.

Only these variable names ever reach the lego process, so a credential cannot set `LD_PRELOAD`,
`PATH` or anything else that changes how lego runs. Some documented variables are held back too:
profile and config-file variables are not offered, and a path variable must name a file inside
`/data/secrets`, so admin input cannot make lego read, write or run anything else.
"""

import functools
import json
import re
import sys
from collections.abc import Mapping
from pathlib import Path

from pydantic import BaseModel, ConfigDict, TypeAdapter

from homeproxy.util.run import run

CATALOG_FILE = Path(__file__).with_name("lego_providers.json")
CLOUDFLARE = "cloudflare"
SELF_SIGNED = "self-signed"
"""Not a lego provider: HomeProxy makes the certificate itself. lego codes have no hyphen, so the
value cannot clash with one."""
REFUSED = frozenset({"exec", "manual"})
"""`exec` runs any program, and `manual` waits for a person to press Enter."""
REFUSED_VARIABLES = frozenset(
    {"AWS_PROFILE", "AWS_SDK_LOAD_CONFIG", "OCI_CONFIG_FILE", "OCI_PROFILE"}
)
"""They make lego load a profile from a config file, and an AWS profile can run a program."""
PATH_VARIABLES = frozenset(
    {"AKAMAI_EDGERC", "HYPERONE_PASSPORT_LOCATION", "INFOBLOX_CA_CERTIFICATE"}
)
"""Variables that hold a file path, besides every name that ends in `_FILE` or `_PATH`."""

# Upper case only: lego's lower-case `TF_VAR_*` names are aliases of upper-case ones.
_VARIABLE = re.compile(r'^\s+- "([A-Z][A-Z0-9_]*)":\s*(.*)$')
_SECTIONS = {"Credentials:": "credentials", "Additional Configuration:": "options"}


class LegoVariable(BaseModel):
    """One environment variable lego reads for a provider."""

    model_config = ConfigDict(frozen=True)

    name: str
    description: str


class LegoProvider(BaseModel):
    """A lego DNS provider. `credentials` are the variables lego lists first, `options` the rest."""

    model_config = ConfigDict(frozen=True)

    code: str
    name: str
    credentials: list[LegoVariable]
    options: list[LegoVariable]


_CATALOG = TypeAdapter(list[LegoProvider])


def _checked_in() -> list[LegoProvider]:
    return _CATALOG.validate_json(CATALOG_FILE.read_bytes())


def catalog() -> list[LegoProvider]:
    """Return the offered providers and their variables, without `REFUSED` and
    `REFUSED_VARIABLES`."""
    return [
        LegoProvider(
            code=p.code,
            name=p.name,
            credentials=[v for v in p.credentials if v.name not in REFUSED_VARIABLES],
            options=[v for v in p.options if v.name not in REFUSED_VARIABLES],
        )
        for p in _checked_in()
        if p.code not in REFUSED
    ]


@functools.cache
def variables() -> dict[str, frozenset[str]]:
    """Return the variable names each offered provider may pass to lego, by provider code.

    Only the names are kept: the API and DNS processes stay within their memory budget.
    """
    return {p.code: frozenset(v.name for v in (*p.credentials, *p.options)) for p in catalog()}


def is_path_variable(name: str) -> bool:
    """Return whether lego reads a file path from this variable."""
    return name.endswith(("_FILE", "_PATH")) or name in PATH_VARIABLES


def check_paths(env: Mapping[str, str], secrets_dir: Path) -> None:
    """Raise `ValueError` unless each path variable names a file inside `secrets_dir` other than
    `secrets.yaml`, which lego could overwrite (acme-dns saves its account to its path).

    The path is resolved, so `..` and symbolic links that lead out of the directory are refused.
    """
    root = secrets_dir.resolve()
    for name, value in env.items():
        path = Path(value)
        if not is_path_variable(name):
            continue
        if not (path.is_absolute() and root in path.resolve().parents):
            raise ValueError(f"{name} must be the path of a file in {secrets_dir}")
        if path.resolve() == root / "secrets.yaml":
            raise ValueError(f"{name} must not be secrets.yaml itself")


def removed(old: list[LegoProvider], new: list[LegoProvider]) -> list[str]:
    """Return what `old` has and `new` lacks, as `<code>` or `<code> <VARIABLE>`.

    A stored domain or credential that uses one would no longer load, so a lego bump that removes
    any needs a migration first.
    """
    fresh = {p.code: {v.name for v in (*p.credentials, *p.options)} for p in new}
    gone = []
    for p in old:
        names = fresh.get(p.code)
        if names is None:
            gone.append(p.code)
        else:
            gone += [
                f"{p.code} {v.name}" for v in (*p.credentials, *p.options) if v.name not in names
            ]
    return gone


def parse_help(code: str, text: str) -> LegoProvider:
    """Return the provider that `lego dnshelp --code <code>` describes in `text`."""
    lines = text.splitlines()
    name = lines[0].removeprefix("Configuration for ").removesuffix(".")
    found: dict[str, list[LegoVariable]] = {"credentials": [], "options": []}
    section: list[LegoVariable] | None = None
    for line in lines:
        if line in _SECTIONS:
            section = found[_SECTIONS[line]]
        elif not line.strip():
            section = None
        elif section is not None and (match := _VARIABLE.match(line)):
            section.append(LegoVariable(name=match[1], description=match[2].strip()))
    return LegoProvider(code=code, name=name, **found)


def read_catalog(binary: str) -> list[LegoProvider]:
    """Return every provider the lego `binary` documents, `exec` and `manual` included."""
    listing = run([binary, "dnshelp"]).stdout
    codes = listing.split("Supported DNS providers:")[1].split("\n\n")[0]
    return [
        parse_help(code, run([binary, "dnshelp", "--code", code]).stdout)
        for code in (c.strip() for c in codes.split(","))
    ]


def main() -> None:
    """Print the catalog of the lego binary named in the first argument as JSON.

    Exits with an error and prints no catalog if that lego lacks a provider or a variable of the
    checked-in catalog.
    """
    fresh = read_catalog(sys.argv[1])
    gone = removed(_checked_in(), fresh)
    if gone:
        sys.exit(f"this lego removed {', '.join(gone)}: migrate domains and credentials first")
    json.dump(_CATALOG.dump_python(fresh, mode="json"), sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
