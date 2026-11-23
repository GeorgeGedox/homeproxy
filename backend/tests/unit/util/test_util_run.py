"""`run` takes fixed argument lists only, and keeps secrets out of results, errors and logs."""

import logging
import os
import secrets
import subprocess
import sys
import time
import traceback
from pathlib import Path

import pytest

from homeproxy.util.redact import REDACTED, register_secret
from homeproxy.util.run import (
    CommandFailed,
    CommandNotStarted,
    CommandTimedOut,
    RunError,
    RunResult,
    run,
)

PYTHON = sys.executable


def python(code: str, *arguments: str) -> list[str]:
    """Return argv for a Python one-liner, so tests need no other binaries."""
    return [PYTHON, "-c", code, *arguments]


@pytest.fixture
def no_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if anything tries to start a process."""

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("a process was started")

    monkeypatch.setattr(subprocess, "Popen", forbidden)


@pytest.mark.parametrize(
    "command",
    ["echo hello", "", b"echo hello", None, 5, {"echo": "hello"}, iter(["echo", "hello"])],
    ids=["str", "empty-str", "bytes", "none", "int", "dict", "iterator"],
)
@pytest.mark.usefixtures("no_spawn")
def test_rejects_anything_but_a_list_or_tuple(command: object) -> None:
    """String commands and other shapes raise before a process can start."""
    with pytest.raises(TypeError, match="argv must be a list or tuple"):
        run(command)  # type: ignore[arg-type]


def test_string_error_does_not_echo_the_command() -> None:
    """The rejected string can hold user input, or a secret. The error stays clean."""
    with pytest.raises(TypeError) as caught:
        run("echo private-value")  # type: ignore[arg-type]
    assert "private-value" not in str(caught.value)


@pytest.mark.parametrize(
    "command", [["echo", 5], ["echo", None], [b"echo"], ["echo", Path("x")], [["echo"]]]
)
@pytest.mark.usefixtures("no_spawn")
def test_rejects_non_string_arguments(command: list[object]) -> None:
    """Every argument must be a string."""
    with pytest.raises(TypeError, match=r"argv\[\d\] must be a string"):
        run(command)  # type: ignore[arg-type]


@pytest.mark.parametrize("command", [[], ()])
@pytest.mark.usefixtures("no_spawn")
def test_rejects_empty_argv(command: list[str] | tuple[str, ...]) -> None:
    """There is no program to run."""
    with pytest.raises(ValueError, match="must not be empty"):
        run(command)


def test_shell_metacharacters_stay_literal(tmp_path: Path) -> None:
    """User values with shell syntax arrive as one argument, and nothing else runs."""
    marker = tmp_path / "marker"
    payload = f"x; touch {marker}; $(touch {marker}) `touch {marker}` | touch {marker} && a > b"
    result = run(python("import sys; print(sys.argv[1])", payload))
    assert result.stdout == f"{payload}\n"
    assert not marker.exists()


def test_accepts_tuple_and_returns_result() -> None:
    """A successful command returns its status and both streams as text."""
    code = "import sys; print('out'); print('err', file=sys.stderr)"
    assert run((PYTHON, "-c", code)) == RunResult(
        argv=(PYTHON, "-c", code), returncode=0, stdout="out\n", stderr="err\n"
    )


def test_non_utf8_output_does_not_raise() -> None:
    """Binary noise from a command must not crash the caller."""
    result = run(python("import sys; sys.stdout.buffer.write(b'a\\xffb')"))
    assert result.stdout == "a�b"


def test_stdin_is_empty_under_a_parent_with_live_stdin() -> None:
    """A command must not inherit a stdin that stays open, or a read would hang it."""
    reader = "import sys; print(repr(sys.stdin.read()))"
    parent = (
        "import sys\n"
        "from homeproxy.util.run import run\n"
        f"print(run([sys.executable, '-c', {reader!r}], timeout=5).stdout)\n"
    )
    read_end, write_end = os.pipe()
    try:
        completed = subprocess.run(  # noqa: S603 - fixed arguments in a test
            [PYTHON, "-c", parent],
            stdin=read_end,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    finally:
        os.close(read_end)
        os.close(write_end)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.startswith("''")


def test_env_adds_to_the_inherited_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Commands like lego need PATH and HOME as well as the added variables."""
    monkeypatch.setenv("HOMEPROXY_TEST_INHERITED", "kept")
    code = "import os; print(os.environ['HOMEPROXY_TEST_INHERITED'], os.environ['ADDED'])"
    assert run(python(code), env={"ADDED": "value"}).stdout == "kept value\n"


def test_without_inheritance_only_the_given_variables_reach_the_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """lego gets nothing from this process, so no variable an admin did not choose reaches it."""
    monkeypatch.setenv("HOMEPROXY_TEST_INHERITED", "kept")
    names = "{'ADDED', 'SECRET', 'HOMEPROXY_TEST_INHERITED'}"
    code = f"import os; print(sorted(os.environ.keys() & {names}))"
    result = run(
        python(code),
        env={"ADDED": "a"},
        secret_env={"SECRET": "run-test-secret"},
        inherit_env=False,
    )
    assert result.stdout == "['ADDED', 'SECRET']\n"


def test_failure_raises_with_output() -> None:
    """A non-zero exit raises, with the status and the output on the exception."""
    code = "import sys; print('out'); print('bad thing', file=sys.stderr); sys.exit(3)"
    with pytest.raises(CommandFailed) as caught:
        run(python(code))
    assert isinstance(caught.value, RunError)
    assert caught.value.returncode == 3
    assert caught.value.stdout == "out\n"
    assert caught.value.stderr == "bad thing\n"
    assert "exited with 3" in str(caught.value)
    assert "bad thing" in str(caught.value)


def test_check_false_returns_the_failure() -> None:
    """Callers such as `haproxy -c` read the status themselves."""
    result = run(python("import sys; sys.exit(3)"), check=False)
    assert result.returncode == 3


def test_missing_binary_raises_not_started(tmp_path: Path) -> None:
    """A missing binary is a RunError, not a raw OSError."""
    with pytest.raises(CommandNotStarted, match="could not start") as caught:
        run([str(tmp_path / "missing")])
    assert caught.value.__context__ is None


def test_timeout_kills_and_raises() -> None:
    """A stuck command is killed well before it would finish."""
    started = time.monotonic()
    with pytest.raises(CommandTimedOut, match="timed out after") as caught:
        run(["sleep", "60"], timeout=0.2)
    assert time.monotonic() - started < 30
    assert caught.value.timeout == 0.2


def test_secret_env_reaches_the_command_but_not_the_result() -> None:
    """The command gets the real value. Everything that comes back is redacted."""
    token = secrets.token_hex(16)
    code = "import os, sys; print(os.environ['T']); print(os.environ['T'], file=sys.stderr)"
    result = run(python(code), secret_env={"T": token})
    assert result.stdout == f"{REDACTED}\n"
    assert result.stderr == f"{REDACTED}\n"


def test_secret_env_value_is_delivered_unchanged(tmp_path: Path) -> None:
    """Redaction must not touch the value the command receives."""
    token = secrets.token_hex(16)
    copy = tmp_path / "copy"
    code = "import os, pathlib, sys; pathlib.Path(sys.argv[1]).write_text(os.environ['T'])"
    run(python(code, str(copy)), secret_env={"T": token})
    assert copy.read_text() == token


@pytest.mark.parametrize("outcome", ["failure", "timeout", "not-started"])
def test_secret_is_not_in_the_error_its_traceback_or_the_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, outcome: str
) -> None:
    """A secret in `secret_env`, echoed by the command or put in argv by mistake, never reaches
    the error, its output attributes, its traceback or the log, even at DEBUG level."""
    token = secrets.token_hex(16)
    register_secret(token)
    echo = 'echo "$T"; echo "bad $T" >&2'
    commands: dict[str, tuple[list[str], type[RunError]]] = {
        "failure": (["sh", "-c", f"{echo}; exit 1", token], CommandFailed),
        "timeout": (["sh", "-c", f"{echo}; exec sleep 60", token], CommandTimedOut),
        "not-started": ([str(tmp_path / "missing"), token], CommandNotStarted),
    }
    argv, expected = commands[outcome]
    with (
        caplog.at_level(logging.DEBUG, logger="homeproxy.util.run"),
        pytest.raises(expected) as caught,
    ):
        run(argv, timeout=2, secret_env={"T": token})
    error = caught.value
    logging.getLogger("homeproxy.test").error("failed", exc_info=error)

    assert error.__context__ is None
    assert "running" in caplog.text
    output = [getattr(error, "stdout", ""), getattr(error, "stderr", "")]
    traceback_text = "".join(traceback.format_exception(error))
    for text in (str(error), repr(error), repr(error.argv), traceback_text, caplog.text, *output):
        assert token not in text
    if outcome != "not-started":
        assert output == [f"{REDACTED}\n", f"bad {REDACTED}\n"]


@pytest.mark.parametrize(
    ("command", "env"),
    [
        (["a\0b"], None),
        ([PYTHON, "a\ud800b"], None),
        ([PYTHON, "--version"], {"BAD=NAME": "x"}),
    ],
    ids=["nul-in-argv", "lone-surrogate-in-argv", "bad-env-name"],
)
def test_arguments_the_os_cannot_take_raise_not_started(
    command: list[str], env: dict[str, str] | None
) -> None:
    """These fail inside subprocess. Callers only have to handle RunError."""
    with pytest.raises(
        CommandNotStarted, match="argument or environment variable is not valid"
    ) as caught:
        run(command, env=env)
    assert caught.value.__context__ is None
