"""The escaping filters make a value literal, or refuse it."""

from collections.abc import Callable

import pytest

from homeproxy.generator.escape import (
    GeneratorError,
    number,
    quoted,
    quoted_log_format,
    token,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("plain", '"plain"'),
        ("", '""'),
        ('a"b', r'"a\"b"'),
        ("a\\", r'"a\\"'),
        ('\\"', r'"\\\""'),
        ("$HOME", r'"\$HOME"'),
        ("${HOME}", r'"\${HOME}"'),
        ("'#", '"\'#"'),
        ("100%", '"100%"'),
        ("/%0a", '"/%0a"'),
    ],
)
def test_quoted_escapes_what_the_config_parser_reads(value: str, expected: str) -> None:
    assert quoted(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("100%", '"100%%"'),
        ("%[env(HOME)]", '"%%[env(HOME)]"'),
        ("%%", '"%%%%"'),
        ("%t", '"%%t"'),
        ('%"$\\', r'"%%\"\$\\"'),
    ],
)
def test_quoted_log_format_also_escapes_percent(value: str, expected: str) -> None:
    assert quoted_log_format(value) == expected


@pytest.mark.parametrize("value", ["a\nb", "a\rb", "a\x00b", "a\tb", "a\x7fb", "café"])
@pytest.mark.parametrize("escape", [quoted, quoted_log_format])
def test_quoted_refuses_what_it_cannot_make_literal(
    value: str, escape: Callable[[str], str]
) -> None:
    with pytest.raises(GeneratorError) as error:
        escape(value)
    assert value not in str(error.value)


@pytest.mark.parametrize(
    "value",
    ["jellyfin", "route-a-", "a.example.com", "192.168.1.20:8080", "fd00::/8", "/data/x", "10s"],
)
def test_token_accepts_plain_words(value: str) -> None:
    assert token(value) == value


@pytest.mark.parametrize(
    "value",
    ["", "a b", "a\nb", "a\n", 'a"', "a'", "a#b", "a%b", "a$b", "a\\b", "-option", "a;b", "{a}"],
)
def test_token_refuses_anything_that_is_not_one_plain_word(value: str) -> None:
    with pytest.raises(GeneratorError) as error:
        token(value)
    assert not value or value not in str(error.value)


def test_number_accepts_only_an_integer() -> None:
    assert number(8443) == "8443"
    for value in (True, "8443", 1.5):
        with pytest.raises(GeneratorError):
            number(value)  # type: ignore[arg-type]
