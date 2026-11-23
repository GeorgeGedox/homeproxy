"""Client for HAProxy's two Unix sockets: the master CLI and the runtime API.

`HAProxyClient.reload` talks to the master CLI, and every other method to the runtime API.
Each call opens its own connection and blocks, so a client can be shared between threads.
Call it from a worker thread, never from an event loop.

Behavior of `haproxy:3.4`, confirmed by `tests/integration/haproxy/`:

- `reload` answers `Success=1` or `Success=0`, then `--` and the startup log, then the
  connection closes. A failed reload leaves the previous worker serving.
- After `Success=1` the old worker can still accept runtime API connections for a few
  milliseconds, until it handles the master's stop signal. `reload` waits for the new worker,
  so the next call never reads or changes the old one.
- The master socket can refuse connections while a reload runs, so an overlapping reload
  can fail with `HAProxyConnectionError`. The apply pipeline's lock keeps them apart. The
  runtime API socket keeps answering throughout.
- Runtime map names and certificate names are the names used in the config, and are not
  resolved: with `default-path parent`, `map(hosts.map)` is `hosts.map`, not its full path.

The runtime API reads a line of space-separated words. A line feed ends a command, `;`
starts another one, and a final `<<` starts a payload. Every word sent here goes through
`_word`, which rejects control characters and backslash-escapes the rest, and a certificate
goes through `_pem_payload`, because an empty line ends a payload. Error messages never
repeat a rejected value or a payload. Answers are parsed as HAProxy sent them, and only the
text that reaches an error message or `ReloadResult.startup_log` goes through `redact`, so a
short registered secret such as `1` can never break parsing.
"""

import contextlib
import re
import socket
import time
from collections.abc import Sequence
from dataclasses import dataclass

from homeproxy.haproxy.stats import (
    HAProxyInfo,
    StatRow,
    StatsParseError,
    parse_info,
    parse_stat,
)
from homeproxy.util.redact import redact

MASTER_SOCKET = "/run/haproxy/master.sock"
ADMIN_SOCKET = "/run/haproxy/admin.sock"
DEFAULT_COMMAND_TIMEOUT = 10.0
# A reload answers only after the new worker has loaded its config, and a large map makes
# that slow. Giving up early would report a failure for a reload that goes on to succeed.
DEFAULT_RELOAD_TIMEOUT = 300.0
_WORKER_POLL_INTERVAL = 0.05

_RECEIVE_SIZE = 65536
_ERROR_TEXT_LIMIT = 2000
# C0 and C1 controls and DEL. A line feed or NUL here would end the command early.
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
# Everything else outside this set gets a backslash. Non-ASCII passes through as UTF-8.
_NEEDS_ESCAPE = re.compile(r"[^A-Za-z0-9._:/=+,\-\u00a0-\U0010ffff]")
_RELOAD_ANSWER = re.compile(r"Success=([01])\n(?:--\n(.*))?", re.DOTALL)
_NEW_VERSION = re.compile(r"New version created: ([0-9]+)\n\n")
_MAP_LINE = re.compile(r"0x[0-9a-fA-F]+ ([^ ]+)(?: (.*))?", re.DOTALL)


class HAProxyError(Exception):
    """A HAProxy socket call failed. The text from HAProxy in it has secrets redacted."""


class HAProxyConnectionError(HAProxyError):
    """A socket could not be reached or stopped answering, or the call timed out.

    After a `reload` this means the result is unknown, not that the reload failed. The
    reload may still finish, so read `show_info` before relying on either outcome.
    """


class HAProxyCommandError(HAProxyError):
    """HAProxy understood the request and refused it. The message holds its answer."""


class HAProxyProtocolError(HAProxyError):
    """HAProxy's answer was empty, cut short or not in the expected format."""


class InvalidArgumentError(HAProxyError, ValueError):
    """A value cannot be sent safely. Nothing was sent, and the message omits the value."""


@dataclass(frozen=True)
class ReloadResult:
    """The result of a master CLI reload.

    `startup_log` holds the alerts that explain a failure. It can quote lines of the
    config, and only secrets registered with `register_secret` are redacted from it.
    """

    success: bool
    startup_log: str


@dataclass(frozen=True)
class MapEntry:
    """One entry of a runtime map."""

    key: str
    value: str


@dataclass(frozen=True)
class HAProxyClient:
    """Calls to one HAProxy instance. Failures raise a subclass of `HAProxyError`.

    Runtime changes are lost at the next reload unless the same change is written to the
    live release files. That is the caller's job.
    """

    master_socket: str = MASTER_SOCKET
    admin_socket: str = ADMIN_SOCKET
    command_timeout: float = DEFAULT_COMMAND_TIMEOUT
    reload_timeout: float = DEFAULT_RELOAD_TIMEOUT

    def reload(self) -> ReloadResult:
        """Reload through the master CLI and wait for the result.

        `Success=0` is a normal result, not an exception. The previous worker keeps
        serving, and a later reload can recover. `Success=1` returns once the runtime API
        answers from the new worker.

        Raises:
            HAProxyConnectionError: No result arrived, so the outcome is unknown. This
                includes a connection closed with no answer at all, and a runtime API that
                still answers from the old worker `command_timeout` seconds after `Success=1`.
            HAProxyProtocolError: The answer is not empty but has no `Success=` result,
                for example a message that another reload is still running.
        """
        before = self._worker_pid()
        answer = _exchange(self.master_socket, b"reload\n", self.reload_timeout)
        if not answer:
            raise HAProxyConnectionError(
                f"{self.master_socket} closed the connection without a reload result"
            )
        match = _RELOAD_ANSWER.fullmatch(answer)
        if match is None:
            raise HAProxyProtocolError(f"unexpected answer to reload: {_shown(answer)}")
        result = ReloadResult(success=match[1] == "1", startup_log=redact((match[2] or "").strip()))
        if result.success and before is not None:
            self._wait_for_new_worker(before)
        return result

    def show_info(self) -> HAProxyInfo:
        """Return `show info` from the current worker."""
        answer = self._read("show info")
        try:
            return parse_info(answer)
        except StatsParseError as exc:
            raise HAProxyProtocolError(f"{exc}: {_shown(answer)}") from None

    def show_stat(self) -> list[StatRow]:
        """Return `show stat`, one row per frontend, backend, server and listener."""
        answer = self._read("show stat")
        try:
            return parse_stat(answer)
        except StatsParseError as exc:
            raise HAProxyProtocolError(f"{exc}: {_shown(answer)}") from None

    def show_servers_state(self) -> str:
        """Return `show servers state`, in the format HAProxy reads from `server-state-file`."""
        return self._read("show servers state")

    def show_map(self, map_name: str) -> list[MapEntry]:
        """Return the entries of a runtime map, in HAProxy's order.

        Raises:
            HAProxyCommandError: HAProxy has no map with this name.
        """
        answer = self._read(f"show map {_reference(map_name, 'map name')}")
        entries = []
        for line in answer.split("\n"):
            if not line:
                continue
            match = _MAP_LINE.fullmatch(line)
            if match is None:
                raise HAProxyCommandError(f"show map failed: {_shown(answer)}")
            entries.append(MapEntry(key=match[1], value=match[2] or ""))
        return entries

    def add_map(self, map_name: str, key: str, value: str) -> None:
        """Add an entry to a runtime map.

        An existing key is not replaced: HAProxy keeps a second entry, and lookups return
        the first. Use `set_map` for a key that exists.
        """
        words = f"{_reference(map_name, 'map name')} {_key(key)} {_word(value, 'map value')}"
        self._write(f"add map {words}", "add map")

    def set_map(self, map_name: str, key: str, value: str) -> None:
        """Change the value of an existing map key.

        Raises:
            HAProxyCommandError: The key or the map does not exist.
        """
        words = f"{_reference(map_name, 'map name')} {_key(key)} {_word(value, 'map value')}"
        self._write(f"set map {words}", "set map")

    def del_map(self, map_name: str, key: str) -> None:
        """Delete one entry of a map key. If `add_map` added the key twice, one stays.

        Raises:
            HAProxyCommandError: The key or the map does not exist.
        """
        self._write(f"del map {_reference(map_name, 'map name')} {_key(key)}", "del map")

    def replace_acl(self, acl_name: str, patterns: Sequence[str]) -> None:
        """Replace every pattern of a runtime ACL file at once.

        The new patterns go into a new version, and lookups see the old ones until the commit.
        A failure before the commit leaves the old patterns in service.

        Raises:
            InvalidArgumentError: `patterns` is empty, which would match nothing. Nothing was sent.
            HAProxyCommandError: HAProxy has no ACL with this name, or refused a pattern.
        """
        if not patterns:
            raise InvalidArgumentError("an ACL must keep at least one pattern")
        name = _reference(acl_name, "ACL name")
        words = [_word(pattern, "ACL pattern") for pattern in patterns]
        answer = self._runtime(f"prepare acl {name}\n")
        match = _NEW_VERSION.fullmatch(answer)
        if match is None:
            raise HAProxyCommandError(f"prepare acl failed: {_shown(answer)}")
        for word in words:
            self._write(f"add acl @{match[1]} {name} {word}", "add acl")
        self._write(f"commit acl @{match[1]} {name}", "commit acl")

    def clear_acl(self, acl_name: str) -> None:
        """Remove every pattern of a runtime ACL file, so it matches nothing.

        Raises:
            HAProxyCommandError: HAProxy has no ACL with this name.
        """
        self._write(f"clear acl {_reference(acl_name, 'ACL name')}", "clear acl")

    def set_ssl_cert(self, name: str, pem: str) -> None:
        """Open a certificate transaction with a new PEM bundle. Nothing is served yet.

        Follow with `commit_ssl_cert` or `abort_ssl_cert`, or use `replace_ssl_cert`.

        Args:
            name: The certificate's name in the config. It must already be loaded, since
                a new certificate needs a reload.
            pem: The certificate chain and private key. Empty lines are removed, because
                an empty line would end the payload.

        Raises:
            HAProxyCommandError: The certificate is not loaded, or the PEM is not valid.
        """
        command = f"set ssl cert {_reference(name, 'certificate name')} <<"
        request = f"{command}\n{_pem_payload(pem)}\n\n"
        answer = self._runtime(request)
        if not answer.startswith(("Transaction created", "Transaction updated")):
            raise HAProxyCommandError(f"set ssl cert failed: {_shown(answer)}")

    def commit_ssl_cert(self, name: str) -> None:
        """Start serving the certificate of an open transaction.

        A failed commit keeps the old certificate in service. The transaction may stay
        open, so call `abort_ssl_cert` afterwards.
        """
        answer = self._runtime(f"commit ssl cert {_reference(name, 'certificate name')}\n")
        if not answer.rstrip().endswith("Success!"):
            raise HAProxyCommandError(f"commit ssl cert failed: {_shown(answer)}")

    def abort_ssl_cert(self, name: str) -> None:
        """Drop an open certificate transaction. Raises if none is open."""
        answer = self._runtime(f"abort ssl cert {_reference(name, 'certificate name')}\n")
        if not answer.startswith("Transaction aborted"):
            raise HAProxyCommandError(f"abort ssl cert failed: {_shown(answer)}")

    def replace_ssl_cert(self, name: str, pem: str) -> None:
        """Hot-swap a loaded certificate, with no reload. See `set_ssl_cert` for the args.

        On failure the old certificate keeps serving, and a best-effort abort closes any
        transaction this call may have opened. That includes a set that HAProxy accepted but
        never confirmed, for example on a timeout. An open transaction would block the hot
        swap of every other certificate until the next reload.

        Raises:
            InvalidArgumentError: The name or the PEM cannot be sent. Nothing was sent.
            HAProxyCommandError: HAProxy refused the PEM, for example a key that does not
                match the certificate.
            HAProxyConnectionError: HAProxy did not answer. The abort was still tried.
        """
        try:
            self.set_ssl_cert(name, pem)
            self.commit_ssl_cert(name)
        except InvalidArgumentError:
            # Nothing was sent, so there is nothing to abort, and an abort could drop a
            # transaction that someone else opened.
            raise
        except HAProxyError:
            # Some failures leave no transaction open, so abort may fail too. The first
            # error is the one that explains what happened.
            with contextlib.suppress(HAProxyError):
                self.abort_ssl_cert(name)
            raise

    def _worker_pid(self) -> int | None:
        """Return the pid of the worker that answers the runtime API, or None if none does."""
        try:
            return self.show_info().pid
        except HAProxyError:
            return None

    def _wait_for_new_worker(self, old: int) -> None:
        """Poll until a worker other than `old` answers the runtime API."""
        deadline = time.monotonic() + self.command_timeout
        while self._worker_pid() in (old, None):
            if time.monotonic() >= deadline:
                raise HAProxyConnectionError(
                    f"the reload succeeded, but the old worker {old} still answered the "
                    f"runtime API after {self.command_timeout} s"
                )
            time.sleep(_WORKER_POLL_INTERVAL)

    def _runtime(self, request: str) -> str:
        """Send a request to the runtime API and return its answer, which is never empty."""
        answer = _exchange(self.admin_socket, request.encode("utf-8"), self.command_timeout)
        if not answer:
            raise HAProxyProtocolError("the runtime API closed the connection without answering")
        return answer

    def _read(self, command: str) -> str:
        """Run a command that returns data. Its answer always ends in an empty line."""
        answer = self._runtime(f"{command}\n")
        if answer != "\n" and not answer.endswith("\n\n"):
            raise HAProxyProtocolError(f"incomplete answer to {command}")
        return answer

    def _write(self, command: str, name: str) -> None:
        """Run a command that answers only with an empty line when it succeeds."""
        answer = self._runtime(f"{command}\n")
        if answer != "\n":
            raise HAProxyCommandError(f"{name} failed: {_shown(answer)}")


def _exchange(path: str, request: bytes, timeout: float) -> str:
    """Send one request over a new connection and read until HAProxy closes it.

    `timeout` covers the whole exchange. The write side stays open: the runtime API and a
    reload both close the connection themselves, and a half-close is not needed.
    """
    deadline = time.monotonic() + timeout
    chunks = []
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout)
            connection.connect(path)
            connection.sendall(request)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                connection.settimeout(remaining)
                chunk = connection.recv(_RECEIVE_SIZE)
                if not chunk:
                    break
                chunks.append(chunk)
    except TimeoutError:
        # Partial output is dropped, since it is no use to anyone without the rest.
        raise HAProxyConnectionError(f"no answer from {path} within {timeout} s") from None
    except OSError as exc:
        # Some errors, such as a socket path that is too long, have no `strerror`.
        reason = exc.strerror or exc
        raise HAProxyConnectionError(f"cannot talk to {path}: {reason}") from None
    return b"".join(chunks).decode("utf-8", "replace")


def _shown(answer: str) -> str:
    """Return HAProxy's answer in a form fit for an error message, with secrets redacted."""
    return redact(answer.strip())[:_ERROR_TEXT_LIMIT]


def _word(value: str, what: str) -> str:
    """Return a value as one literal word of a runtime API command, or raise."""
    if not value:
        raise InvalidArgumentError(f"{what} must not be empty")
    if _CONTROL.search(value):
        raise InvalidArgumentError(f"{what} contains a control character")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise InvalidArgumentError(f"{what} is not valid text") from None
    return _NEEDS_ESCAPE.sub(lambda match: f"\\{match[0]}", value)


def _key(key: str) -> str:
    """Return a map key as one word, or raise.

    `show map` cannot tell where a key with a space ends. After unescaping, `set map` and
    `del map` read a leading `#` as a reference to an entry, so `#0x55e8...` would change
    or delete some other entry. A `#` line in a map file is also a comment, so such a key
    could never be written to the release files.
    """
    if " " in key:
        raise InvalidArgumentError("map key contains a space")
    if key.startswith("#"):
        raise InvalidArgumentError("map key starts with #")
    return _word(key, "map key")


def _reference(name: str, what: str) -> str:
    """Return a map or certificate name as one word.

    After unescaping, HAProxy reads a leading `#` as a numeric id and a leading `@` as a
    version, so neither can be part of a name.
    """
    if " " in name or name.startswith(("#", "@")):
        raise InvalidArgumentError(f"{what} is not a file name")
    return _word(name, what)


def _pem_payload(pem: str) -> str:
    """Return a PEM bundle as a payload that cannot end early. It is never shown in errors."""
    lines = [line.strip() for line in pem.replace("\r\n", "\n").split("\n")]
    payload = "\n".join(line for line in lines if line)
    if not payload:
        raise InvalidArgumentError("certificate payload is empty")
    if _CONTROL.search(payload.replace("\n", "")):
        raise InvalidArgumentError("certificate payload contains a control character")
    try:
        payload.encode("utf-8")
    except UnicodeEncodeError:
        raise InvalidArgumentError("certificate payload is not valid text") from None
    return payload
