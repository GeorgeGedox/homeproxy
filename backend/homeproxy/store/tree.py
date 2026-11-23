"""Load and write YAML as a ruamel round-trip tree, which keeps comments, key order and quotes.

To change a file, edit its tree and write the tree. Dumping a model instead loses comments and key
order and adds nulls.

The output style is two-space mappings and sequences indented by two. A file in another style keeps
its comments, key order and values, and takes this style on its first write. A leading `---` line,
CRLF line ends and a UTF-8 byte order mark are dropped. ruamel loses a comment after a `...` line
or above a `%YAML` line. An emptied mapping or sequence is written as `{}` or `[]`, without the
comment that sat above its first entry.
"""

import re
import warnings
from io import BytesIO
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.error import YAMLFutureWarning, YAMLWarning

from homeproxy.store.errors import ParseError, SerializeError
from homeproxy.util.atomic import atomic_write

# ruamel's warnings quote source lines, for example on a reused anchor, so a warning while parsing
# `secrets.yaml` could print a secret to stderr. Every ruamel warning derives from one of these two
# classes. The filters are process-wide: `catch_warnings` around each parse would not be
# thread-safe, and its exit could remove another caller's filters or keep its own for good.
_WARNING_CATEGORIES: tuple[type[Warning], ...] = (YAMLWarning, YAMLFutureWarning)
_IGNORE_FILTERS = [("ignore", None, category, None, 0) for category in _WARNING_CATEGORIES]


def _ignore_ruamel_warnings() -> None:
    """Put the ignore filters at the front of the process-wide list, unless they are there.

    The import adds them, and each parse checks again. An import inside another caller's
    `catch_warnings` block loses them when that block exits, as pytest's collection does, and a
    later `simplefilter("always")` would otherwise take priority.
    """
    head = warnings.filters[: len(_IGNORE_FILTERS)]
    if all(item in head for item in _IGNORE_FILTERS):
        return
    for category in _WARNING_CATEGORIES:
        # This removes an equal filter further down before it inserts at the front.
        warnings.filterwarnings("ignore", category=category)


_ignore_ruamel_warnings()

_BOM = b"\xef\xbb\xbf"

_DOCUMENT_START = re.compile(rb"---[ \t]*(?P<comment>#.*)?")


def _new_yaml() -> YAML:
    """Return a new instance, because a `YAML` object keeps state and is not thread-safe."""
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    # Never fold a long line, so a write changes only the lines that were edited.
    yaml.width = 1 << 30
    yaml.indent(mapping=2, sequence=4, offset=2)
    return yaml


def _without_document_start(raw: bytes) -> tuple[bytes, int]:
    """Remove a leading `---` line, and return the text and the number of lines removed.

    ruamel drops every comment above a `---` line, and it would drop the marker on output anyway.
    A comment on the marker line stays, as a line of its own.
    """
    lines = raw.split(b"\n")
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped and not stripped.startswith(b"#"):
            marker = _DOCUMENT_START.fullmatch(line)
            if marker is None:
                break
            if marker["comment"]:
                lines[index] = marker["comment"]
                return b"\n".join(lines), 0
            del lines[index]
            return b"\n".join(lines), 1
    return raw, 0


def parse_tree(raw: bytes, file: str) -> CommentedMap:
    """Parse YAML whose top level must be a mapping. `file` names it in errors.

    Every failure raises `ParseError` with a position at most, because ruamel's own text can quote
    the file. ruamel's warnings quote it too, so the module ignores them. A UTF-8 byte order mark
    is dropped.
    """
    failure: ParseError
    raw = raw.removeprefix(_BOM).replace(b"\r\n", b"\n")
    text, removed_lines = _without_document_start(raw)
    _ignore_ruamel_warnings()
    try:
        tree = _new_yaml().load(text)
    except Exception as error:
        mark = getattr(error, "problem_mark", None)
        if mark is None:
            failure = ParseError(file)
        else:
            failure = ParseError(file, mark.line + 1 + removed_lines, mark.column + 1)
    else:
        if isinstance(tree, CommentedMap):
            return tree
        failure = ParseError(file, reason="the top level must be a mapping")
    raise failure


def load_tree(path: Path) -> CommentedMap:
    """Read a file as a tree. A missing file raises `FileNotFoundError`."""
    return parse_tree(path.read_bytes(), path.name)


def _flow_empty_containers(node: CommentedMap | CommentedSeq) -> None:
    """Write every empty mapping and sequence under `node` as `{}` or `[]` on its key, in place.

    ruamel writes an emptied block container below the comment that sat above its first entry, as
    `{}` or `[]` at a column no parser reads back. So that comment is dropped, as its entries are
    gone, and the container is switched to flow style. A comment on the key's own line is kept.
    """
    children = node.items() if isinstance(node, CommentedMap) else enumerate(node)
    for key, child in children:
        if isinstance(child, CommentedMap | CommentedSeq):
            _flow_empty_containers(child)
            if not child:
                child.fa.set_flow_style()
                if key in node.ca.items:
                    node.ca.items[key][3] = None


def dump_tree(tree: CommentedMap, file: str) -> bytes:
    """Return a tree as YAML. Failures raise `SerializeError`, as ruamel's text shows values.

    Empty mappings and sequences in the tree are set to flow style, so the output parses again.
    """
    buffer = BytesIO()
    failed = False
    try:
        _flow_empty_containers(tree)
        _new_yaml().dump(tree, buffer)
    except Exception:
        failed = True
    if failed:
        raise SerializeError(file)
    return buffer.getvalue()


def write_tree(path: Path, tree: CommentedMap, *, mode: int | None = None) -> None:
    """Write a tree atomically. See `atomic_write` for `mode`. This does not validate the tree."""
    atomic_write(path, dump_tree(tree, path.name), mode=mode)
