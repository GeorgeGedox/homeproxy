"""The one way to start a subprocess: a fixed argument list, no shell, output redacted."""

import logging
import os
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass

from homeproxy.util.redact import redact, register_secret

DEFAULT_TIMEOUT = 60.0
_MESSAGE_OUTPUT_LIMIT = 2000

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RunResult:
    """A finished command. Every field has secrets redacted."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


class RunError(Exception):
    """A command did not succeed. The message and attributes hide registered secrets.

    A value that was never registered is shown as it is.
    """

    def __init__(self, message: str, argv: tuple[str, ...]) -> None:
        """Store the redacted command with a message that is already redacted."""
        super().__init__(message)
        self.argv = argv


class CommandNotStarted(RunError):
    """The command could not start, for example a missing binary or working directory."""


class CommandFailed(RunError):
    """The command exited with a non-zero status, or a signal killed it."""

    def __init__(self, result: RunResult) -> None:
        """Build the message from a result, with the end of its stderr."""
        tail = result.stderr[-_MESSAGE_OUTPUT_LIMIT:]
        message = f"command {list(result.argv)!r} exited with {result.returncode}: {tail}"
        super().__init__(message, result.argv)
        self.returncode = result.returncode
        self.stdout = result.stdout
        self.stderr = result.stderr


class CommandTimedOut(RunError):
    """The command ran past its timeout and was killed. Output is what it wrote before that."""

    def __init__(self, argv: tuple[str, ...], timeout: float, stdout: str, stderr: str) -> None:
        """Store the timeout with the partial output."""
        super().__init__(f"command {list(argv)!r} timed out after {timeout} s", argv)
        self.timeout = timeout
        self.stdout = stdout
        self.stderr = stderr


def _validate_argv(argv: object) -> tuple[str, ...]:
    """Return argv as a tuple, or raise. Error messages never echo the arguments."""
    if not isinstance(argv, list | tuple):
        raise TypeError(
            f"argv must be a list or tuple of strings, not {type(argv).__name__}; "
            "shell command strings are not accepted"
        )
    if not argv:
        raise ValueError("argv must not be empty")
    for index, argument in enumerate(argv):
        if not isinstance(argument, str):
            raise TypeError(f"argv[{index}] must be a string, not {type(argument).__name__}")
    return tuple(argv)


def _partial_output(output: bytes | str | None) -> str:
    """Decode the output on a timeout, which subprocess leaves as bytes even in text mode."""
    if output is None:
        return ""
    text = output.decode("utf-8", "replace") if isinstance(output, bytes) else output
    return redact(text)


def run(
    argv: list[str] | tuple[str, ...],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    check: bool = True,
    env: Mapping[str, str] | None = None,
    secret_env: Mapping[str, str] | None = None,
    inherit_env: bool = True,
) -> RunResult:
    """Run a command from a fixed argument list and capture its output as text.

    A shell is never involved. A string command raises `TypeError` before anything
    starts, because a string is what a shell command built from user input looks like.
    stdin is empty, so a command that asks for input cannot hang.

    Args:
        argv: The program and its arguments. Put no secret here, because any process
            can read a command line. Use `secret_env`.
        timeout: Seconds before the command is killed and `CommandTimedOut` is raised.
        check: Raise `CommandFailed` on a non-zero exit. With `False`, read `returncode`.
        env: Variables added to the inherited environment.
        secret_env: Like `env`, for secrets. The values are registered with
            `register_secret` before the command starts, so they stay out of logs,
            of the result and of errors, even if the command echoes them.
        inherit_env: With `False`, the command gets only `env` and `secret_env`, and
            nothing from this process's environment.

    Returns:
        The result, with secrets redacted from the arguments and the output.

    Raises:
        TypeError: argv is not a list or tuple of strings.
        ValueError: argv is empty.
        CommandNotStarted: The operating system could not start the command, or the
            arguments or environment cannot be passed to it, for example a NUL byte.
        CommandFailed: The command failed and `check` is true.
        CommandTimedOut: The command ran past `timeout`.
    """
    command = _validate_argv(argv)
    inherited = os.environ if inherit_env else {}
    environment = {**inherited, **(env or {}), **(secret_env or {})}
    if secret_env:
        register_secret(*secret_env.values())

    # Redact here too, so the safety of these lines does not rest on the logging hook.
    shown = tuple(redact(argument) for argument in command)
    logger.debug("running %r", list(shown))
    started = time.monotonic()
    # Raised after the except blocks, never inside them. The raw exception holds raw
    # arguments and raw output, and `from None` would still keep it in __context__.
    failure: RunError | None = None
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argument list, never a shell
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=environment,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        failure = CommandTimedOut(
            shown, timeout, _partial_output(exc.stdout), _partial_output(exc.stderr)
        )
    except (OSError, ValueError, UnicodeError) as exc:
        # The text of these exceptions can hold a raw path or argument, so it is not used.
        reason = (
            redact(str(exc.strerror))
            if isinstance(exc, OSError)
            else "an argument or environment variable is not valid"
        )
        failure = CommandNotStarted(f"could not start {list(shown)!r}: {reason}", shown)
    if failure is not None:
        raise failure

    result = RunResult(
        argv=shown,
        returncode=completed.returncode,
        stdout=redact(completed.stdout),
        stderr=redact(completed.stderr),
    )
    logger.debug(
        "exit %d after %.3f s from %r, stderr: %r",
        result.returncode,
        time.monotonic() - started,
        list(shown),
        result.stderr[-_MESSAGE_OUTPUT_LIMIT:],
    )
    if check and result.returncode != 0:
        raise CommandFailed(result)
    return result
