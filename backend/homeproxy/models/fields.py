"""Validated field types. These are the first defense against HAProxy directive injection.

A newline or quote in a value that reaches `haproxy.cfg` could add a directive, so every type here
allows a small set of characters. The escaping filter in the generator is the second defense.

Pattern rules:

- Patterns go to the JSON schema, so the browser runs them too. They use no lookaround, and `[0-9]`
  rather than `\\d`, so that pydantic's Rust regex engine and JavaScript agree.
- Without a multiline flag, `$` in both engines matches only at the very end of the text. Python's
  `re` differs: its `$` also matches before a trailing newline, so Python code here uses
  `re.fullmatch`.
- `ipaddress` is never the only check. It accepts IPv6 scope IDs holding any character, such as
  `fe80::1%a\\nb`, so a pattern always runs first.
- Error messages from these validators never include the value, because it may be a secret.
- Every pattern comes with plain words for the user, through `plain_pattern`, so a refused value
  never shows the regex.
"""

import base64
import binascii
import ipaddress
import re
from typing import Annotated, Literal

from pydantic import AfterValidator, BeforeValidator, Field, SecretStr, StringConstraints

_LABEL = r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
_IPV4_SHAPE = r"[0-9]{1,3}(\.[0-9]{1,3}){3}"
_PORT = r"[1-9][0-9]{0,4}"
_NUMERIC_LABEL = re.compile(r"[0-9]+|0x[0-9a-f]*")
_V4_MAPPED = ipaddress.IPv6Network("::ffff:0:0/96")

PATTERN_MESSAGES: dict[str, str] = {}
"""What each pattern asks for, in plain words, by pattern. `safe_errors` shows these."""


def plain_pattern(pattern: str, message: str, max_length: int | None = None) -> StringConstraints:
    """Return the constraint for `pattern`, and record `message` as what it asks for."""
    PATTERN_MESSAGES[pattern] = message
    return StringConstraints(max_length=max_length, pattern=pattern)


def _check_ipv4(value: str) -> None:
    try:
        ipaddress.IPv4Address(value)
    except ValueError:
        raise ValueError("not a valid IPv4 address") from None


def _ipv4(value: str) -> str:
    _check_ipv4(value)
    return value


def _ipv4_or_keyword(keyword: str) -> AfterValidator:
    def check(value: str) -> str:
        if value != keyword:
            _check_ipv4(value)
        return value

    return AfterValidator(check)


def _check_port(port: str) -> None:
    if int(port) > 65535:
        raise ValueError("port must be between 1 and 65535")


def _ends_in_number(host: str) -> bool:
    """Return whether the resolver could read this host as a number, such as `0x7f000001`.

    The C resolver accepts decimal, octal and hex parts, and all parts must be numeric, so the last
    label decides. A middle label such as the one in `0x1.example.com` is an ordinary name.
    """
    return _NUMERIC_LABEL.fullmatch(host.rpartition(".")[2]) is not None


def _check_host(host: str) -> None:
    """Allow a numeric host only as a canonical IPv4 address, not as `999.1.1.1` or `0300.0.0.1`."""
    if _ends_in_number(host):
        _check_ipv4(host)


def _hostname(value: str) -> str:
    if _ends_in_number(value):
        raise ValueError("must be a DNS name, not an address")
    return value


def _exact_int(value: object) -> object:
    """Accept only a plain integer in a file.

    `Literal[1]` also matches `True` and `1.0`, which compare equal to 1. ruamel loads an anchored
    `true` as `ScalarBoolean`, an int subclass that passes pydantic's strict integer check.
    """
    if type(value) is not int:
        raise ValueError("must be an integer")
    return value


def _host_port(value: str) -> str:
    host, _, port = value.rpartition(":")
    _check_host(host)
    _check_port(port)
    return value


def _ipv4_port(value: str) -> str:
    host, _, port = value.rpartition(":")
    _check_ipv4(host)
    _check_port(port)
    return value


def _url_host_port(value: str) -> str:
    authority = value.partition("://")[2].partition("/")[0]
    host, colon, port = authority.partition(":")
    _check_host(host)
    if colon:
        _check_port(port)
    return value


def _ip_or_cidr(value: str) -> str:
    try:
        network = ipaddress.ip_network(value, strict=True)
    except ValueError:
        try:
            ipaddress.ip_network(value, strict=False)
        except ValueError:
            raise ValueError("not a valid IP address or CIDR range") from None
        raise ValueError(
            "has host bits set: a range starts at its network address, as in 192.168.1.0/24"
        ) from None
    # Covering all of `::ffff:0:0/96` is the IPv4 catch-all in IPv6 notation.
    if network.prefixlen == 0 or (network.version == 6 and _V4_MAPPED.subnet_of(network)):
        raise ValueError("a catch-all range would match every client")
    return value


def unique(values: list[str]) -> list[str]:
    """Reject a list that holds the same value twice."""
    if len(set(values)) != len(values):
        raise ValueError("values must be unique")
    return values


FileVersion = Annotated[Literal[1], BeforeValidator(_exact_int)]
"""The `version` field of every file. The store migrates older files before validation."""

Name = Annotated[
    str,
    plain_pattern(
        r"^[a-z0-9][a-z0-9-]{0,62}$",
        "must be lowercase letters, digits and hyphens, at most 63, not starting with a hyphen",
    ),
]
"""A route or credentials name. A route name is also its file name and backend name."""

Hostname = Annotated[
    str,
    plain_pattern(
        rf"^{_LABEL}(\.{_LABEL})+$",
        "must be a DNS name in lowercase letters, digits, dots and hyphens, as in app.example.com",
        max_length=253,
    ),
    AfterValidator(_hostname),
]
"""A lowercase DNS name with at least two labels, such as a route host or a zone. No wildcards."""

Port = Annotated[int, BeforeValidator(_exact_int), Field(ge=1, le=65535)]

IPv4 = Annotated[
    str,
    plain_pattern(rf"^{_IPV4_SHAPE}$", "must be an IPv4 address, such as 192.168.1.10"),
    AfterValidator(_ipv4),
]

IpOrCidr = Annotated[
    str,
    plain_pattern(
        r"^[0-9a-fA-F:.]+(/(0|[1-9][0-9]{0,2}))?$",
        "must be an IP address or a range, such as 192.168.1.10 or 192.168.1.0/24",
    ),
    AfterValidator(_ip_or_cidr),
]
"""An IPv4 or IPv6 address or range, without host bits set. Catch-all ranges are rejected, because
they would make LAN-only routes public or skip CrowdSec for every client."""

ListenAddress = Annotated[
    str,
    plain_pattern(
        rf"^{_IPV4_SHAPE}:{_PORT}$", "must be an IPv4 address and a port, such as 192.168.1.10:8080"
    ),
    AfterValidator(_ipv4_port),
]
"""`ip:port` for a local listener. Listeners are IPv4 only in v1."""

HostPort = Annotated[
    str,
    plain_pattern(
        rf"^{_LABEL}(\.{_LABEL})*:{_PORT}$",
        "must be a lowercase host and a port, such as 192.168.1.10:8080 or nas:80",
        max_length=259,
    ),
    AfterValidator(_host_port),
]
"""`host:port`, where host is an IPv4 address or a name. A compose service name has one label."""

BaseUrl = Annotated[
    str,
    plain_pattern(
        rf"^https?://{_LABEL}(\.{_LABEL})*(:{_PORT})?$",
        "must be an http:// or https:// address with a lowercase host and no path, such as"
        " http://192.168.1.10:8080",
        max_length=267,
    ),
    AfterValidator(_url_host_port),
]
"""An `http` or `https` URL with a host and optional port, and no path, query or credentials."""

RedirectUrl = Annotated[
    str,
    plain_pattern(
        rf"^https?://{_LABEL}(\.{_LABEL})*(:{_PORT})?(/[A-Za-z0-9._~%/?&=+:@,;!-]*)?$",
        "must be an http:// or https:// address with a lowercase host, such as"
        " https://example.com/path, and no spaces, quotes or #",
        max_length=2048,
    ),
    AfterValidator(_url_host_port),
]
"""An absolute `http` or `https` URL that a redirect route sends clients to. It reaches a map file,
so it holds no whitespace, quotes, `#` or `[`, and so never `%[`. A path starts with `/`."""

RedirectCode = Annotated[Literal[301, 302, 307, 308], BeforeValidator(_exact_int)]

Duration = Annotated[
    str,
    plain_pattern(
        r"^[1-9][0-9]{0,5}(ms|s|m|h|d)$",
        "must be a number and a unit (ms, s, m, h or d), such as 10s",
    ),
]
"""A positive time in HAProxy's format, such as `10s` or `12h`."""

_DURATION = re.compile(r"([1-9][0-9]*)(ms|s|m|h|d)")
_DURATION_MS = {"ms": 1, "s": 1_000, "m": 60_000, "h": 3_600_000, "d": 86_400_000}


def duration_ms(text: str) -> int:
    """Return a time in HAProxy's format, such as `10s` or `12h`, in milliseconds. Raises
    `ValueError` for anything else."""
    match = _DURATION.fullmatch(text)
    if match is None:
        raise ValueError("not a duration")
    return int(match[1]) * _DURATION_MS[match[2]]


def _upper(value: object) -> object:
    # Only ASCII: `upper` turns `ß` into `SS`, a real code.
    return value.upper() if isinstance(value, str) and value.isascii() else value


CountryCode = Annotated[
    str,
    plain_pattern(r"^[A-Z]{2}$", "must be a two-letter country code, such as DE"),
    BeforeValidator(_upper),
]
"""A country code in the GeoIP data's format. Lowercase input is uppercased. Whether the data has
the code is checked when a block list is saved, so loading config never depends on the data."""

HttpPath = Annotated[
    str,
    plain_pattern(
        r"^/[A-Za-z0-9._~%/?&=+-]*$",
        "must start with / and hold only letters, digits and . _ ~ % / ? & = + -",
        max_length=256,
    ),
]
"""A request path for a health check."""

PathPrefix = Annotated[
    str,
    plain_pattern(
        # Segments of letters, digits, `.`, `_` and `-`, but never `.` or `..` alone.
        r"^(/|(/(\.{0,2}[A-Za-z0-9_-]|\.{3})[A-Za-z0-9._-]*)+)$",
        "must be / or a path such as /app, of letters, digits and . _ -, with no trailing /",
        max_length=256,
    ),
]
"""The path an `http` route serves on its hosts: `/`, or whole segments without a trailing `/`.
It reaches a map file and a regex-free HAProxy rule, so it holds no `%`, quotes or spaces."""

HeaderName = Annotated[
    str,
    plain_pattern(
        r"^[A-Za-z0-9][A-Za-z0-9-]*$",
        "must be letters, digits and hyphens, such as X-Frame-Options",
        max_length=128,
    ),
]

HeaderValue = Annotated[
    str,
    plain_pattern(r"^[ -~]+$", "must be printable ASCII characters on one line", max_length=1024),
]
"""Printable ASCII, 0x20 to 0x7E. Quotes are allowed because values such as `default-src 'self'`
need them, so the generator's escaping filter must escape them."""

DnsTarget = Annotated[
    str,
    plain_pattern(
        rf"^(haproxy|{_IPV4_SHAPE})$", "must be haproxy or an IPv4 address, such as 192.168.1.10"
    ),
    _ipv4_or_keyword("haproxy"),
]
"""`haproxy` answers with the proxy's LAN IP. An IPv4 address answers with that address."""

PublicIp = Annotated[
    str,
    plain_pattern(rf"^(auto|{_IPV4_SHAPE})$", "must be auto or an IPv4 address"),
    _ipv4_or_keyword("auto"),
]
"""`auto` detects the WAN address. An IPv4 address fixes it."""

BasicAuthUser = Annotated[
    str,
    plain_pattern(
        r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$",
        "must be letters, digits, dots, hyphens and underscores, at most 64, starting with a letter"
        " or digit",
    ),
]


_ARGON2ID = re.compile(
    r"\$argon2id\$v=[0-9]+\$m=[0-9]+,t=[0-9]+,p=[0-9]+\$[A-Za-z0-9+/]+\$[A-Za-z0-9+/]+"
)
SHA512_CRYPT_HASH = re.compile(
    r"\$6\$(rounds=([0-9]{1,4}|[1-4][0-9]{4}|50000)\$)?[./A-Za-z0-9]{1,16}\$[./A-Za-z0-9]{86}"
)
"""A SHA-512 crypt hash with at most 50,000 rounds. HAProxy runs `crypt` on its event loop for every
request with credentials, and passlib's default of 656,000 rounds costs about 0.3 s each."""
_TOKEN = re.compile(r"[!#-&(-~]*")


def _password_hash(value: SecretStr) -> SecretStr:
    if _ARGON2ID.fullmatch(value.get_secret_value()) is None:
        raise ValueError("must be an argon2id hash in PHC format")
    return value


def _basic_auth_hash(value: SecretStr) -> SecretStr:
    if SHA512_CRYPT_HASH.fullmatch(value.get_secret_value()) is None:
        raise ValueError("must be a SHA-512 crypt hash with at most 50000 rounds")
    return value


def _token(value: SecretStr) -> SecretStr:
    secret = value.get_secret_value()
    if _TOKEN.fullmatch(secret) is None:
        raise ValueError("must be printable ASCII without spaces or quotes")
    # JSON dumps show a secret as stars. Sending them back must not overwrite the real value.
    if secret and not secret.strip("*"):
        raise ValueError("must not be the masked placeholder")
    return value


def _required(value: SecretStr) -> SecretStr:
    if not value.get_secret_value():
        raise ValueError("must not be empty")
    return value


def _session_key(value: SecretStr) -> SecretStr:
    try:
        decoded = base64.b64decode(value.get_secret_value().encode("ascii"), validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("must be base64") from None
    if len(decoded) < 32:
        raise ValueError("must decode to at least 32 bytes")
    return value


def _signing_key(value: SecretStr) -> SecretStr:
    if value.get_secret_value() and len(value.get_secret_value()) < 32:
        raise ValueError("must be empty or at least 32 characters")
    return value


PasswordHash = Annotated[SecretStr, AfterValidator(_password_hash)]
"""The dashboard password as an argon2id hash. Masked in `repr` and JSON dumps."""

BasicAuthHash = Annotated[SecretStr, AfterValidator(_basic_auth_hash)]
"""A route password as a SHA-512 crypt hash, the format HAProxy can check quickly on each request.
It reaches the generated userlist, so its format is strict."""

SecretToken = Annotated[SecretStr, AfterValidator(_token)]
"""An optional secret. It may be empty. It reaches generated files, so its characters are strict."""

RequiredSecretToken = Annotated[SecretStr, AfterValidator(_token), AfterValidator(_required)]
"""A secret that must have a value."""

SessionKey = Annotated[SecretStr, AfterValidator(_session_key)]
"""At least 32 random bytes in base64."""

SigningKey = Annotated[SecretStr, AfterValidator(_token), AfterValidator(_signing_key)]
"""An optional key that is at least 32 bytes when set."""
