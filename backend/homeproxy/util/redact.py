"""Secret redaction for logs, errors, API responses and diffs.

Whoever loads a secret calls `register_secret` with it. From that moment the value
is replaced in every record that goes through `logging`, in this process, whichever
logger or handler carries the record. Text that leaves by another route, such as an
API response or a diff, must go through `redact` or `redact_data`.

Only register values that are secret. `redact` replaces every occurrence, so
registering `cloudflare` or `homeproxy-ui` from `secrets.yaml` would garble logs. Values
shorter than `MIN_LENGTH` are ignored as a last line of defense: no real secret is that short,
and replacing every `1` or `s` would garble every log line.

Registration is per process. Each process that handles a secret registers it itself.

Known limits. These can still put a secret in a log:

- A `Logger` subclass that overrides `handle` without calling `super`.
- A direct call to `Handler.handle` or `Logger.callHandlers`, which skips the hook.
- A field that a filter adds to a record after the hook has run.
- The logger name, printed by `%(name)s`, which is not scrubbed.
- A secret split across record fields or across lines.
- A truncated prefix of a secret.
- Base64 and case-changed spellings of a secret.
- `warnings.warn` without `logging.captureWarnings`, which never reaches `logging`.
"""

import contextlib
import json
import logging
import re
import threading
from collections.abc import Callable, Iterator, Mapping
from urllib.parse import quote, quote_plus

REDACTED = "[REDACTED]"
MIN_LENGTH = 3

_lock = threading.Lock()
_needles: set[str] = set()
# Replaced whole, never changed in place, so readers need no lock.
_pattern: re.Pattern[str] | None = None
_hook_installed = False

_STANDARD_RECORD_ATTRIBUTES = frozenset(vars(logging.makeLogRecord({}))) | {"message", "asctime"}

_ENCODERS: tuple[Callable[[str], str], ...] = (
    # %r and json.dumps escape quotes, backslashes and newlines, so a dict in a log
    # line or a JSON body would not contain the raw value.
    lambda value: repr(value)[1:-1],
    lambda value: json.dumps(value)[1:-1],
    lambda value: json.dumps(value, ensure_ascii=False)[1:-1],
    # HTTP clients and servers log URLs. uvicorn logs paths with the default safe="/",
    # a query string encodes "/" too, and a form body encodes spaces as "+".
    quote,
    lambda value: quote(value, safe=""),
    quote_plus,
)


def _encoded_forms(value: str) -> set[str]:
    """Return the spellings of a secret that text can hold, always with the raw value."""
    return {value} | {encode(value) for encode in _ENCODERS}


def register_secret(*values: str) -> None:
    """Mark values as secret and turn on log redaction for the whole process.

    Registration is permanent, so a rotated token stays redacted. A value is also
    registered without its trailing newlines, which a YAML block or a file read leaves
    on it. Values shorter than `MIN_LENGTH` without those newlines are ignored.
    """
    global _pattern
    fresh: set[str] = set()
    for value in values:
        stripped = value.rstrip("\r\n")
        if len(stripped) >= MIN_LENGTH:
            fresh |= _encoded_forms(value) | _encoded_forms(stripped)
    with _lock:
        if fresh <= _needles:
            return
        _needles.update(fresh)
        # Longest first, so a secret inside a longer secret cannot leave a tail behind.
        ordered = sorted(_needles, key=len, reverse=True)
        _pattern = re.compile("|".join(re.escape(needle) for needle in ordered))
        _install_log_hook()


@contextlib.contextmanager
def restored_secrets() -> Iterator[None]:
    """Forget every secret registered inside the block when it ends.

    Registration is otherwise permanent. Tests use this, so one test's secrets never redact
    text in a later test.
    """
    global _pattern
    with _lock:
        needles, pattern = set(_needles), _pattern
    try:
        yield
    finally:
        with _lock:
            _needles.clear()
            _needles.update(needles)
            _pattern = pattern


def redact(text: str) -> str:
    """Return text with every registered secret replaced by `REDACTED`."""
    pattern = _pattern
    return text if pattern is None else pattern.sub(REDACTED, text)


def redact_data(value: object) -> object:
    """Return a copy of JSON-like data with every registered secret replaced.

    Walks strings, mappings (keys too), lists, tuples, sets and frozensets. Any other
    object is returned as it is, unless its `str()` holds a secret, when it becomes the
    redacted string. Nothing else about an object is inspected, so its attributes and
    its `repr()` can still hold a secret. Convert objects to plain data first.
    """
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, Mapping):
        return {redact_data(key): redact_data(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_data(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_data(item) for item in value)
    if isinstance(value, set):
        return {redact_data(item) for item in value}
    if isinstance(value, frozenset):
        return frozenset(redact_data(item) for item in value)
    text = str(value)
    clean = redact(text)
    return value if clean == text else clean


def _scrub_fields(record: logging.LogRecord) -> None:
    """Replace secrets in every field of a log record that a formatter can print."""
    try:
        message = record.getMessage()
    except Exception:
        # The handler would hit the same error, and Handler.handleError prints the raw
        # msg and args to stderr. Flatten them here so that report is clean too.
        message = f"{record.msg} {record.args!r}"
        record.msg, record.args = redact(message), ()
    clean = redact(message)
    shown_args = repr(record.args)
    # Also check args on their own: a format string can skip an arg, and
    # Handler.handleError prints all of them. Records without a secret keep their args,
    # because uvicorn's AccessFormatter unpacks them.
    if clean != message or redact(shown_args) != shown_args:
        record.msg, record.args = clean, ()

    if record.exc_info:
        trace = logging.Formatter().formatException(record.exc_info)
        clean = redact(trace)
        if clean != trace:
            # Formatter prints exc_text. Dropping exc_info stops it from formatting again.
            record.exc_text, record.exc_info = clean, None
    if record.exc_text:
        record.exc_text = redact(record.exc_text)
    if record.stack_info:
        record.stack_info = redact(record.stack_info)

    for key, value in list(vars(record).items()):
        if key not in _STANDARD_RECORD_ATTRIBUTES:
            record.__dict__[key] = redact_data(value)


def _scrub_record(record: logging.LogRecord) -> None:
    """Scrub a record in place. Never raises, and never lets a record through unscrubbed."""
    try:
        _scrub_fields(record)
    except Exception:
        # A broken __str__ must not fail the caller, and the record's content is unknown.
        record.msg, record.args = "log record dropped: redaction failed", ()
        record.exc_info, record.exc_text, record.stack_info = None, None, None
        for key in list(vars(record)):
            if key not in _STANDARD_RECORD_ATTRIBUTES:
                record.__dict__[key] = REDACTED


def _install_log_hook() -> None:
    """Wrap `Logger.handle`, once. The caller holds `_lock`.

    Normal logging calls and `Logger.handle` calls reach this method before any handler
    sees the record. The module docstring lists the routes that do not. The documented
    hooks each leave a gap: a logger filter skips records from child loggers, a handler
    filter misses handlers added later (uvicorn adds its own), and the record factory
    runs before `extra` is copied onto the record.
    """
    global _hook_installed
    if _hook_installed:
        return
    original = logging.Logger.handle

    def handle(self: logging.Logger, record: logging.LogRecord) -> None:
        _scrub_record(record)
        original(self, record)

    logging.Logger.handle = handle  # type: ignore[method-assign]
    _hook_installed = True
