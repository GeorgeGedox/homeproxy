"""The escaping filters. Every value in a generated HAProxy file passes through one of them.

The validators in `homeproxy.models.fields` are the first defense against directive injection, and
these filters are the second. A filter never trusts that a model validated its value, because
`model_construct` and `model_copy(update=...)` skip validation. A value a filter cannot make safe
raises `GeneratorError`, and the message never holds the value.

HAProxy reads a config line in two steps, and a value must survive both:

1. The config parser splits the line into words. Inside double quotes, `\\` starts an escape, `$`
   starts an environment variable and `"` ends the word. `#` and `'` are ordinary characters there.
2. Some keywords then read a word as a log-format string, where `%` starts an expression. `%%` is
   a literal `%`. `set-header` is one of these keywords. `http-check send uri` is not.

Each filter returns `Escaped`. The template environment refuses to output anything else, so a
template that forgets a filter fails instead of writing a raw value.
"""

import re


class GeneratorError(Exception):
    """The generator cannot produce a safe config. The message holds no user value."""


class Escaped(str):
    """Text that an escaping filter produced and that is safe to write to a config file."""

    __slots__ = ()


_TOKEN = re.compile(r"[A-Za-z0-9:/][A-Za-z0-9._:/-]*")
_PRINTABLE_ASCII = re.compile(r"[ -~]*")
_CONFIG_SPECIAL = re.compile(r'[\\"$]')
_REDIRECT_URL = re.compile(r"https?://[A-Za-z0-9._~%/?&=+:@,;!-]+")
_PATH_PREFIX = re.compile(r"/|(/(\.{0,2}[A-Za-z0-9_-]|\.{3})[A-Za-z0-9._-]*)+")


def token(value: str) -> Escaped:
    """Return a value that needs no quotes, such as a name, host, address, range or duration.

    Only letters, digits and `.`, `_`, `:`, `/`, `-` are allowed, so the value stays one word and
    cannot start an option. Raises `GeneratorError` for anything else.
    """
    if _TOKEN.fullmatch(value) is None:
        raise GeneratorError("a name, host or address holds a character that is not allowed")
    return Escaped(value)


def number(value: int) -> Escaped:
    """Return an integer such as a port. Raises `GeneratorError` for any other type."""
    if type(value) is not int:
        raise GeneratorError("a number has the wrong type")
    return Escaped(str(value))


def redirect_url(value: str) -> Escaped:
    """Return a redirect target for `redirect.map`, where it is the rest of a line and later the
    text of a `Location` header.

    Only an `http` or `https` URL of URL characters is allowed: no whitespace, quotes, `#`, `|` or
    `[`, so never `%[`. Raises `GeneratorError` for anything else.
    """
    if _REDIRECT_URL.fullmatch(value) is None:
        raise GeneratorError("a redirect target holds a character that is not allowed")
    return Escaped(value)


def path_prefix(value: str) -> Escaped:
    """Return a route's path prefix: `/`, or whole segments of letters, digits, `.`, `_` and `-`
    without a trailing `/`, and never a `.` or `..` segment.

    It is one word, and starts with `/`, so after a host it can never name another host. Raises
    `GeneratorError` for anything else.
    """
    if _PATH_PREFIX.fullmatch(value) is None:
        raise GeneratorError("a path prefix holds a character that is not allowed")
    return Escaped(value)


def quoted(value: str) -> Escaped:
    """Return a double-quoted word that the config parser reads back as exactly this value.

    Use it where HAProxy does not read the word as a log-format string. Raises `GeneratorError` if
    the value holds anything but printable ASCII, because a newline would end the directive.
    """
    if _PRINTABLE_ASCII.fullmatch(value) is None:
        raise GeneratorError("a quoted value holds a control or non-ASCII character")
    return Escaped('"' + _CONFIG_SPECIAL.sub(lambda match: "\\" + match.group(), value) + '"')


def quoted_log_format(value: str) -> Escaped:
    """Return a double-quoted log-format string that HAProxy sends as exactly this value.

    A `%[...]` in the value stays literal text and never becomes an expression. Use it for a
    `set-header` value. Raises `GeneratorError` as `quoted` does.
    """
    return quoted(value.replace("%", "%%"))


def only_escaped(value: object) -> object:
    """Refuse template output that did not pass through a filter. This is the Jinja2 `finalize`."""
    if not isinstance(value, Escaped):
        raise TypeError("a template wrote a value without an escaping filter")
    return value
