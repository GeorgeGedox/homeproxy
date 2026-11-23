"""Secrets registered with `register_secret` never reach logs, in any field of a record."""

import io
import json
import logging
import logging.config
import os
import secrets
import subprocess
import sys
from collections.abc import Callable, Iterator
from urllib.parse import quote, quote_plus

import pytest
from uvicorn.logging import AccessFormatter

from homeproxy.util.redact import (
    REDACTED,
    redact,
    redact_data,
    register_secret,
    restored_secrets,
)


def new_secret(template: str = "{}") -> str:
    """Register a unique secret. Registration is permanent, so tests never share one."""
    secret = template.format(secrets.token_hex(16))
    register_secret(secret)
    return secret


@pytest.fixture
def logged() -> Iterator[tuple[logging.Logger, io.StringIO]]:
    """Yield a logger that writes to a buffer, as a real handler would."""
    logger = logging.getLogger(f"test.{secrets.token_hex(8)}")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    logger.addHandler(handler)
    yield logger, buffer
    logger.removeHandler(handler)


def test_redact_replaces_each_occurrence() -> None:
    """Every copy of a secret goes, and the text around it stays."""
    secret = new_secret()
    assert redact(f"a {secret} b {secret} c") == f"a {REDACTED} b {REDACTED} c"


def test_redact_leaves_other_text_alone() -> None:
    """Text without a secret comes back unchanged."""
    new_secret()
    assert redact("nothing to see") == "nothing to see"


def test_empty_secret_is_ignored() -> None:
    """An unset optional secret must not match between every character."""
    new_secret()
    register_secret("")
    assert redact("plain") == "plain"


@pytest.mark.parametrize("value", ["1", "ab", "s\n"])
def test_a_value_too_short_to_be_a_secret_is_ignored(value: str) -> None:
    """Replacing every `1` or `s` would garble all text, and no real secret is that short."""
    register_secret(value)
    assert redact("1 ab s") == "1 ab s"


def test_registrations_are_forgotten_after_restored_secrets() -> None:
    """The autouse fixture in `tests/conftest.py` keeps each test's secrets to itself."""
    kept = new_secret()
    with restored_secrets():
        scoped = new_secret()
        assert redact(scoped) == REDACTED
    assert (redact(kept), redact(scoped)) == (REDACTED, scoped)


def test_longer_secret_leaves_no_tail() -> None:
    """A secret inside a longer one must not expose the rest of the longer one."""
    short = secrets.token_hex(8)
    long = f"{short}{secrets.token_hex(8)}"
    register_secret(short)
    register_secret(long)
    assert redact(f"[{long}]") == f"[{REDACTED}]"


@pytest.mark.parametrize(
    "template",
    ['{}"quote', "{}'quote", "{}\\slash", "{}\nnewline", "{}\ttab", "{}é", "{}%s%d", "{}/+ =&"],
)
def test_escaped_spellings_are_redacted(template: str) -> None:
    """repr, JSON and URL encoding must not hide a secret from redaction."""
    secret = new_secret(template)
    for spelling in (secret, repr(secret), json.dumps(secret), quote(secret, safe="")):
        assert secret not in redact(spelling)
        assert redact(spelling).replace(REDACTED, "").strip("'\"") == ""


@pytest.mark.parametrize(
    ("template", "spell"),
    [
        ("{}+/=", quote),
        ("{} /", lambda value: quote(value, safe="")),
        ("{} +", quote_plus),
        ('{}é"', lambda value: json.dumps(value, ensure_ascii=False)[1:-1]),
    ],
    ids=["quote-default-safe", "quote-no-safe", "quote-plus", "json-not-ascii"],
)
def test_each_encoded_form_is_redacted(template: str, spell: Callable[[str], str]) -> None:
    """Each encoder has its own spelling, which no other form matches."""
    secret = new_secret(template)
    spelled = spell(secret)
    assert spelled != secret
    assert redact(f"[{spelled}]") == f"[{REDACTED}]"


@pytest.mark.parametrize("ending", ["\n", "\n\n", "\r\n"])
def test_trailing_newlines_are_not_part_of_the_match(ending: str) -> None:
    """A YAML block or a file read leaves a newline that the logged value does not have."""
    bare = secrets.token_hex(16)
    register_secret(f"{bare}{ending}")
    assert redact(f"[{bare}]") == f"[{REDACTED}]"
    assert redact(f"[{bare}{ending}]") == f"[{REDACTED}]"


@pytest.mark.parametrize("value", ["\n", "\r\n", "\n\n"])
def test_newline_only_value_is_ignored(value: str) -> None:
    """A blank value must not match every line break, or match between every character."""
    new_secret()
    register_secret(value)
    assert redact("a\nb\r\nc") == "a\nb\r\nc"


def test_redact_data_walks_containers() -> None:
    """Values, keys and nested containers are all redacted."""
    secret = new_secret()
    data = {"token": secret, secret: 1, "list": [secret, 2], "tuple": (secret,), "set": {secret}}
    assert redact_data(data) == {
        "token": REDACTED,
        REDACTED: 1,
        "list": [REDACTED, 2],
        "tuple": (REDACTED,),
        "set": {REDACTED},
    }


def test_redact_data_keeps_objects_without_secrets() -> None:
    """Numbers and clean objects keep their type, so a response body stays valid."""
    new_secret()
    marker = object()
    assert redact_data({"count": 3, "none": None, "object": marker}) == {
        "count": 3,
        "none": None,
        "object": marker,
    }


def test_redact_data_flattens_objects_that_hold_secrets() -> None:
    """An object whose string form holds a secret must not survive."""
    secret = new_secret()

    class Holder:
        def __str__(self) -> str:
            return f"holder {secret}"

    assert redact_data(Holder()) == f"holder {REDACTED}"
    assert redact_data(secret.encode()) == f"b'{REDACTED}'"


def test_log_message_text(logged: tuple[logging.Logger, io.StringIO]) -> None:
    """A secret in the message itself is redacted."""
    logger, buffer = logged
    secret = new_secret()
    logger.info(f"token is {secret}")
    assert buffer.getvalue() == f"INFO token is {REDACTED}\n"


def test_log_message_args(logged: tuple[logging.Logger, io.StringIO]) -> None:
    """A secret passed as a lazy format argument is redacted."""
    logger, buffer = logged
    secret = new_secret()
    logger.info("token is %s", secret)
    assert buffer.getvalue() == f"INFO token is {REDACTED}\n"


def test_log_secret_split_across_args(logged: tuple[logging.Logger, io.StringIO]) -> None:
    """Redaction works on the formatted line, so splitting a secret does not hide it."""
    logger, buffer = logged
    secret = new_secret()
    logger.info("%s%s", secret[:8], secret[8:])
    assert buffer.getvalue() == f"INFO {REDACTED}\n"


def test_log_container_arg_with_repr_escapes(logged: tuple[logging.Logger, io.StringIO]) -> None:
    """A dict argument prints its repr, which escapes quotes in the secret."""
    logger, buffer = logged
    secret = new_secret("{}'\"\\")
    logger.info("config %s", {"api_token": secret})
    assert secret not in buffer.getvalue()
    assert repr(secret)[1:-1] not in buffer.getvalue()
    assert REDACTED in buffer.getvalue()


def test_log_secret_with_format_characters(logged: tuple[logging.Logger, io.StringIO]) -> None:
    """A secret holding % must not break formatting after it is replaced."""
    logger, buffer = logged
    secret = new_secret("{}%s%d")
    logger.info("token %s and %d", secret, 5)
    assert buffer.getvalue() == f"INFO token {REDACTED} and 5\n"


def test_log_exception_message_and_chain(logged: tuple[logging.Logger, io.StringIO]) -> None:
    """Tracebacks print exception messages, including those of chained exceptions."""
    logger, buffer = logged
    outer, inner = new_secret(), new_secret()
    try:
        try:
            raise ValueError(f"inner {inner}")
        except ValueError as exc:
            raise RuntimeError(f"outer {outer}") from exc
    except RuntimeError:
        logger.exception("failed")
    output = buffer.getvalue()
    assert outer not in output
    assert inner not in output
    assert f"RuntimeError: outer {REDACTED}" in output
    assert f"ValueError: inner {REDACTED}" in output


def test_log_clean_exception_keeps_traceback(logged: tuple[logging.Logger, io.StringIO]) -> None:
    """An exception without a secret still logs its full traceback."""
    logger, buffer = logged
    new_secret()
    try:
        raise ValueError("clean")
    except ValueError:
        logger.exception("failed")
    assert "Traceback" in buffer.getvalue()
    assert "ValueError: clean" in buffer.getvalue()


def test_log_stack_info_and_prebuilt_exc_text(logged: tuple[logging.Logger, io.StringIO]) -> None:
    """Fields set on a hand-built record are redacted as well."""
    logger, buffer = logged
    secret = new_secret()
    record = logging.makeLogRecord(
        {
            "msg": "m",
            "levelno": logging.INFO,
            "levelname": "INFO",
            "exc_text": f"exc {secret}",
            "stack_info": f"s {secret}",
        }
    )
    logger.handle(record)
    assert secret not in buffer.getvalue()
    assert f"exc {REDACTED}" in buffer.getvalue()
    assert f"s {REDACTED}" in buffer.getvalue()


def test_log_extra_fields(logged: tuple[logging.Logger, io.StringIO]) -> None:
    """`extra` values reach formatters directly, so they are redacted, nested ones too."""
    logger, buffer = logged
    secret = new_secret()
    handler = logger.handlers[0]
    handler.setFormatter(logging.Formatter("%(message)s %(token)s %(nested)s"))
    logger.info("msg", extra={"token": secret, "nested": {"deep": [secret]}})
    assert secret not in buffer.getvalue()
    assert buffer.getvalue().count(REDACTED) == 2


def test_log_object_arg(logged: tuple[logging.Logger, io.StringIO]) -> None:
    """A secret inside an argument's __str__ is redacted."""
    logger, buffer = logged
    secret = new_secret()

    class Holder:
        def __str__(self) -> str:
            return secret

    logger.info("value %s", Holder())
    assert buffer.getvalue() == f"INFO value {REDACTED}\n"


def test_log_bad_format_string_stays_clean(
    logged: tuple[logging.Logger, io.StringIO], capsys: pytest.CaptureFixture[str]
) -> None:
    """A broken format call must not make logging print raw args to stderr."""
    logger, buffer = logged
    secret = new_secret()
    logger.info("count %d", secret)
    captured = capsys.readouterr()
    assert secret not in buffer.getvalue()
    assert secret not in captured.err
    assert REDACTED in buffer.getvalue()


def test_log_record_without_secret_keeps_unused_args(
    logged: tuple[logging.Logger, io.StringIO],
) -> None:
    """The args check flattens only records that hold a secret."""
    logger, _ = logged
    new_secret()
    records: list[logging.LogRecord] = []

    def keep(record: logging.LogRecord) -> bool:
        records.append(record)
        return True

    logger.addFilter(keep)
    logger.info("loaded config", {"count": 1})
    assert records[0].args == {"count": 1}


def test_log_child_logger_and_late_handler() -> None:
    """Loggers and handlers created after registration are covered without setup."""
    secret = new_secret()
    name = f"test.{secrets.token_hex(8)}"
    parent = logging.getLogger(name)
    parent.setLevel(logging.INFO)
    parent.propagate = False
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    parent.addHandler(handler)
    try:
        logging.getLogger(f"{name}.child.grandchild").info("token %s", secret)
    finally:
        parent.removeHandler(handler)
    assert buffer.getvalue() == f"token {REDACTED}\n"


def test_log_reaches_caplog_redacted(caplog: pytest.LogCaptureFixture) -> None:
    """The record object itself is clean, not only the formatted text."""
    secret = new_secret()
    with caplog.at_level(logging.INFO, logger="test.caplog"):
        logging.getLogger("test.caplog").info("token %s", secret)
    assert secret not in caplog.text
    assert secret not in repr(caplog.records[0].args)
    assert secret not in str(caplog.records[0].msg)


def test_clean_uvicorn_access_record_keeps_working() -> None:
    """uvicorn's access formatter unpacks record.args, so clean records must keep them."""
    new_secret()
    logger = logging.getLogger(f"test.{secrets.token_hex(8)}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setFormatter(AccessFormatter("%(request_line)s %(status_code)s", use_colors=False))
    logger.addHandler(handler)
    try:
        logger.info('%s - "%s %s HTTP/%s" %d', "192.168.1.10:1234", "GET", "/api", "1.1", 200)
    finally:
        logger.removeHandler(handler)
    assert buffer.getvalue() == "GET /api HTTP/1.1 200 OK\n"


def test_uvicorn_access_record_with_secret_does_not_leak(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With a secret in the URL the access line is lost, and nothing leaks anywhere."""
    secret = new_secret()
    logger = logging.getLogger(f"test.{secrets.token_hex(8)}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setFormatter(AccessFormatter("%(request_line)s", use_colors=False))
    logger.addHandler(handler)
    try:
        logger.info(
            '%s - "%s %s HTTP/%s" %d', "192.168.1.10:1234", "GET", f"/x?k={secret}", "1.1", 200
        )
    finally:
        logger.removeHandler(handler)
    assert secret not in buffer.getvalue() + capsys.readouterr().err


_CHILD_SCRIPT = """
import logging, logging.config, os, sys
from uvicorn.config import LOGGING_CONFIG
from homeproxy.util.redact import register_secret

secret = os.environ["TEST_SECRET"]
register_secret(secret)

# Configure logging after registration, the way uvicorn does when it starts.
logging.config.dictConfig(LOGGING_CONFIG)
logging.basicConfig(level=logging.DEBUG, stream=sys.stdout, format="%(name)s %(message)s %(key)s",
                    force=True)

extra = {"key": secret}
log = logging.getLogger("homeproxy.child")
log.debug("message %s", secret, extra=extra)
log.warning(secret, extra=extra)
log.error("mapping %r", {"token": secret}, extra=extra)
try:
    raise RuntimeError(secret)
except RuntimeError:
    log.exception("failed", extra=extra)
logging.getLogger("uvicorn.error").error("uvicorn %s", secret)
print("done", flush=True)
"""


def test_secret_never_reaches_real_process_output() -> None:
    """End to end: real stdout and stderr of a process hold no trace of the secret.

    This runs uvicorn's own logging config, so handlers this module never saw are covered.
    """
    secret = secrets.token_hex(16)
    completed = subprocess.run(  # noqa: S603 - fixed arguments in a test
        [sys.executable, "-c", _CHILD_SCRIPT],
        capture_output=True,
        text=True,
        env={**os.environ, "TEST_SECRET": secret},
        check=True,
        timeout=60,
    )
    output = completed.stdout + completed.stderr
    assert "done" in completed.stdout
    assert "homeproxy.child" in completed.stdout
    assert "RuntimeError" in completed.stdout
    assert "uvicorn [REDACTED]" in completed.stderr
    assert secret not in output
