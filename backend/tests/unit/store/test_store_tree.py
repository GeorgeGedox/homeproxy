"""Round-trip keeps comments and key order, and no error or warning quotes the file."""

import secrets
import traceback
import warnings
from pathlib import Path

import pytest
from ruamel.yaml.comments import CommentedMap

from homeproxy.store import ParseError, SerializeError, dump_tree, load_tree, parse_tree, write_tree

DATA = Path(__file__).parents[4] / "examples" / "data"
EXAMPLES = sorted(DATA.rglob("*.yaml"))

HAND_EDITED = b"""\
# Top comment.

# Second block, after a blank line.
zebra: 1 # after a value
apple: "double quoted"
mango: 'single quoted'
list:
  # Above the first item.
  - one
  # Between items.
  - two # after an item
flow: {b: 1, a: 2} # after a flow map
literal: |
  line one
  line two
folded: >-
  folded text
  continues here
base: &base {x: 1}
copy: *base

# Comment above the last key.
last: value
# Comment at the end.
"""


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda path: path.name)
def test_seeded_files_round_trip_byte_for_byte(path: Path) -> None:
    assert dump_tree(load_tree(path), path.name) == path.read_bytes()


def test_hand_edited_file_round_trips_byte_for_byte() -> None:
    assert dump_tree(parse_tree(HAND_EDITED, "f"), "f") == HAND_EDITED


def test_edit_changes_only_the_edited_line() -> None:
    tree = parse_tree(HAND_EDITED, "f")
    tree["zebra"] = 2
    assert dump_tree(tree, "f") == HAND_EDITED.replace(b"zebra: 1", b"zebra: 2")


def test_new_key_goes_last_and_old_keys_keep_their_order() -> None:
    tree = parse_tree(b"zebra: 1 # z\napple: 2 # a\n", "f")
    tree["mango"] = 3
    assert dump_tree(tree, "f") == b"zebra: 1 # z\napple: 2 # a\nmango: 3\n"


EMPTIED = b"""\
section: # on the key
  # above the first entry
  key: 1
items:
  # above the first item
  - 1
list:
  - nested:
      # deep
      key: 1
last:
  # at the end of the file
  key: 1
"""


def test_emptied_commented_containers_read_back() -> None:
    tree = parse_tree(EMPTIED, "f")
    for mapping in (tree["section"], tree["list"][0]["nested"], tree["last"]):
        mapping.clear()
    tree["items"].clear()

    written = dump_tree(tree, "f")

    reread = parse_tree(written, "f")
    assert reread == {"section": {}, "items": [], "list": [{"nested": {}}], "last": {}}
    assert b"section: {} # on the key\n" in written


def test_long_lines_are_not_folded() -> None:
    text = b"key: " + b"word " * 200 + b"end\n"
    assert dump_tree(parse_tree(text, "f"), "f") == text


def test_other_list_indent_keeps_comments_and_values() -> None:
    output = dump_tree(parse_tree(b"# top\nitems:\n- a # first\n- b\n", "f"), "f")
    assert output == b"# top\nitems:\n  - a # first\n  - b\n"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (b"# above\n---\n# below\na: 1\n", b"# above\n# below\na: 1\n"),
        (b"---\na: 1\n", b"a: 1\n"),
        (b"# above\n--- # on the marker\na: 1\n", b"# above\n# on the marker\na: 1\n"),
    ],
)
def test_comments_around_a_document_start_are_kept(raw: bytes, expected: bytes) -> None:
    assert dump_tree(parse_tree(raw, "f"), "f") == expected


def test_value_on_the_document_start_line_is_kept() -> None:
    assert parse_tree(b"--- {a: 1}\n", "f") == {"a": 1}


def test_crlf_file_gets_consistent_line_ends() -> None:
    assert dump_tree(parse_tree(b"a: 1 # c\r\nb: 2\r\n", "f"), "f") == b"a: 1 # c\nb: 2\n"


def test_write_tree_writes_atomically_with_the_given_mode(tmp_path: Path) -> None:
    path = tmp_path / "file.yaml"
    write_tree(path, parse_tree(HAND_EDITED, "f"), mode=0o600)
    assert path.read_bytes() == HAND_EDITED
    assert path.stat().st_mode & 0o777 == 0o600


def test_missing_file_raises_file_not_found(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_tree(tmp_path / "missing.yaml")


def full_text(error: BaseException) -> str:
    return "".join(traceback.format_exception(error)) + repr(vars(error))


BROKEN_TEMPLATES = [
    "password: {secret}\n  bad: [indent\n",
    'password: "{secret}\n',
    "password: {secret}\npassword: {secret}\n",
    "password: {secret}: nested\n",
    "{secret}: 1\n{secret}: 2\n",
    "a: 1\n---\nb: {secret}\n",
    "!!!{secret}\n",
    "a: !{secret}!x y\n: :\n",
    "a: *{secret}\n",
    "%BAD {secret}\n",
    "- {secret}\n",
    "{secret}\n",
    "a: \x07{secret}\n",
    "a: [" * 3000 + "{secret}\n",
]


@pytest.mark.parametrize("template", BROKEN_TEMPLATES)
def test_parse_error_never_holds_file_content(template: str) -> None:
    secret = "topsecret" + secrets.token_hex(16)
    with pytest.raises(ParseError) as caught:
        parse_tree(template.format(secret=secret).encode(), "secrets.yaml")
    error = caught.value
    assert error.__cause__ is None
    assert error.__context__ is None
    assert "secrets.yaml" in str(error)
    assert secret not in full_text(error)


def test_parse_error_gives_the_position_in_the_original_file() -> None:
    with pytest.raises(ParseError) as caught:
        parse_tree(b"# top\n---\na: 1\nb: [\n", "f")
    assert "at line 5, column 1" in str(caught.value)


def test_invalid_utf8_raises_parse_error() -> None:
    with pytest.raises(ParseError):
        parse_tree(b"a: \xff\xfe\xff\n", "f")


def test_impossible_date_raises_parse_error() -> None:
    with pytest.raises(ParseError):
        parse_tree(b"a: 2001-13-45\n", "f")


@pytest.mark.parametrize("raw", [b"", b"# only a comment\n", b"- a\n", b"scalar\n"])
def test_top_level_must_be_a_mapping(raw: bytes) -> None:
    with pytest.raises(ParseError, match="mapping"):
        parse_tree(raw, "f")


def test_file_name_with_a_newline_cannot_forge_a_log_line() -> None:
    with pytest.raises(ParseError) as caught:
        parse_tree(b"a: [", "evil\nFORGED")
    assert "\n" not in str(caught.value)


def test_warning_on_a_reused_anchor_does_not_quote_the_file() -> None:
    secret = "topsecret" + secrets.token_hex(16)
    raw = f"a: &x {secret}\nb: &x {secret}\n".encode()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        parse_tree(raw, "secrets.yaml")
    assert [str(item.message) for item in caught] == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (b"\xef\xbb\xbf# h\n---\na: 1\n", b"# h\na: 1\n"),
        (b"\xef\xbb\xbfa: 1 # c\n", b"a: 1 # c\n"),
    ],
)
def test_byte_order_mark_is_dropped_and_comments_kept(raw: bytes, expected: bytes) -> None:
    assert dump_tree(parse_tree(raw, "f"), "f") == expected


def test_python_tags_do_not_run_code(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    raw = f'a: !!python/object/apply:os.system ["touch {marker}"]\n'.encode()
    tree = parse_tree(raw, "f")
    assert not marker.exists()
    assert not isinstance(tree["a"], str)


def test_serialize_failure_does_not_show_the_value() -> None:
    secret = "topsecret" + secrets.token_hex(16)

    class Unwritable:
        def __repr__(self) -> str:
            return secret

        def __str__(self) -> str:
            return secret

    tree = CommentedMap()
    tree["a"] = Unwritable()
    with pytest.raises(SerializeError) as caught:
        dump_tree(tree, "f")
    assert caught.value.__context__ is None
    assert secret not in full_text(caught.value)
