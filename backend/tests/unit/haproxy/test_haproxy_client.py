"""What the client sends, and what it does with answers a healthy HAProxy never gives.

A small Unix socket server stands in for HAProxy, so these tests can check exact bytes and
force broken answers. HAProxy's own behavior is covered by
`tests/integration/haproxy/test_haproxy_client_live.py`.
"""

import contextlib
import logging
import shutil
import socket
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from homeproxy.haproxy.client import (
    HAProxyClient,
    HAProxyCommandError,
    HAProxyConnectionError,
    HAProxyError,
    HAProxyProtocolError,
    InvalidArgumentError,
    MapEntry,
    ReloadResult,
)
from homeproxy.util.redact import REDACTED, register_secret

PAYLOAD_MARKER = "only-in-the-pem-payload"
PEM = (
    "-----BEGIN CERTIFICATE-----\nQ0VSVA\n-----END CERTIFICATE-----\n"
    f"-----BEGIN TEST KEY-----\n{PAYLOAD_MARKER}\n-----END TEST KEY-----\n"
)
PEM_BODY = PEM.rstrip("\n")


class FakeSocket:
    """Answers each connection with the next scripted reply, then closes it.

    Each connection has its own thread, so a slow answer does not hold up the next
    connection. `delays` sets the delay of the first connections in turn, and `delay`
    that of the rest.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.requests: list[bytes] = []
        self.replies: list[bytes] = []
        self.delays: list[float] = []
        self.delay = 0.0
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(path))
        self.server.listen()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while True:
            try:
                connection, _ = self.server.accept()
            except OSError:
                return
            delay = self.delays.pop(0) if self.delays else self.delay
            threading.Thread(target=self._answer, args=(connection, delay), daemon=True).start()

    def _answer(self, connection: socket.socket, delay: float) -> None:
        with connection:
            data = b""
            # A real server reads a command up to its line end, or to the empty line
            # that ends a payload. The client keeps its side open.
            while not data.endswith(b"\n") or (b"<<\n" in data and b"\n\n" not in data):
                chunk = connection.recv(65536)
                if not chunk:
                    break
                data += chunk
            # Taken in the order requests arrive, before any delay.
            self.requests.append(data)
            reply = self.replies.pop(0) if self.replies else b"\n"
            time.sleep(delay)
            # A client that timed out has gone away already.
            with contextlib.suppress(BrokenPipeError):
                connection.sendall(reply)

    def close(self) -> None:
        self.server.shutdown(socket.SHUT_RDWR)
        self.server.close()
        self.thread.join(timeout=5)


@pytest.fixture
def sockets() -> Iterator[tuple[FakeSocket, FakeSocket]]:
    # Not tmp_path: a Unix socket path is limited to about 100 bytes.
    directory = Path(tempfile.mkdtemp(prefix="hp-"))
    master = FakeSocket(directory / "m.sock")
    admin = FakeSocket(directory / "a.sock")
    yield master, admin
    master.close()
    admin.close()
    shutil.rmtree(directory)


@pytest.fixture
def master(sockets: tuple[FakeSocket, FakeSocket]) -> FakeSocket:
    return sockets[0]


@pytest.fixture
def admin(sockets: tuple[FakeSocket, FakeSocket]) -> FakeSocket:
    return sockets[1]


@pytest.fixture
def client(sockets: tuple[FakeSocket, FakeSocket]) -> HAProxyClient:
    return HAProxyClient(master_socket=str(sockets[0].path), admin_socket=str(sockets[1].path))


# ---- reload ----


def test_reload_sends_only_reload_to_the_master_socket(
    client: HAProxyClient, master: FakeSocket, admin: FakeSocket
) -> None:
    master.replies = [b"Success=1\n--\n[NOTICE] loaded\n\n"]
    assert client.reload() == ReloadResult(success=True, startup_log="[NOTICE] loaded")
    assert master.requests == [b"reload\n"]
    # Only the pid lookup before the reload. With no answer, there is no worker to wait for.
    assert admin.requests == [b"show info\n"]


def info(pid: int) -> bytes:
    return f"Version: 3.4.7\nPid: {pid}\nUptime_sec: 1\n\n".encode()


def test_reload_returns_once_the_new_worker_answers_the_runtime_api(
    client: HAProxyClient, master: FakeSocket, admin: FakeSocket
) -> None:
    """The old worker can answer for a moment after `Success=1`. A caller that read it then
    would see stats without the new backends, or hot-swap a certificate into a dying worker."""
    master.replies = [b"Success=1\n"]
    admin.replies = [info(7), info(7), b"Permission denied\n\n", info(7), info(8)]

    assert client.reload().success is True

    # One lookup before, then polls through the old pid and an error until pid 8 answers.
    assert len(admin.requests) == 5


def test_reload_whose_old_worker_keeps_answering_is_an_unknown_outcome(
    master: FakeSocket, admin: FakeSocket
) -> None:
    client = HAProxyClient(str(master.path), str(admin.path), command_timeout=0.3)
    master.replies = [b"Success=1\n"]
    admin.replies = [info(7)] * 100

    with pytest.raises(HAProxyConnectionError, match="old worker 7 still answered"):
        client.reload()


def test_reload_failure_is_a_result_with_its_log(client: HAProxyClient, master: FakeSocket) -> None:
    master.replies = [b"Success=0\n--\n[ALERT] parsing failed\nnext line\n\n"]
    assert client.reload() == ReloadResult(
        success=False, startup_log="[ALERT] parsing failed\nnext line"
    )


def test_reload_without_a_log_is_still_a_result(client: HAProxyClient, master: FakeSocket) -> None:
    master.replies = [b"Success=1\n"]
    assert client.reload() == ReloadResult(success=True, startup_log="")


@pytest.mark.parametrize(
    "reply",
    [b"\n", b"Another reload is still in progress.\n", b"Success=2\n--\n", b"Success=1"],
)
def test_reload_without_a_result_raises(
    client: HAProxyClient, master: FakeSocket, reply: bytes
) -> None:
    master.replies = [reply]
    with pytest.raises(HAProxyProtocolError):
        client.reload()


def test_reload_closed_with_no_answer_is_an_unknown_outcome(
    client: HAProxyClient, master: FakeSocket
) -> None:
    # The master may have started the reload before the connection dropped.
    master.replies = [b""]
    with pytest.raises(HAProxyConnectionError, match="without a reload result"):
        client.reload()


def test_reload_waits_longer_than_a_command(master: FakeSocket, admin: FakeSocket) -> None:
    client = HAProxyClient(
        master_socket=str(master.path),
        admin_socket=str(admin.path),
        command_timeout=0.05,
        reload_timeout=5.0,
    )
    master.delay = 0.3
    master.replies = [b"Success=1\n--\nslow\n\n"]
    assert client.reload().success


def test_reload_timeout_raises_connection_error(master: FakeSocket, admin: FakeSocket) -> None:
    client = HAProxyClient(
        master_socket=str(master.path), admin_socket=str(admin.path), reload_timeout=0.05
    )
    master.delay = 0.5
    with pytest.raises(HAProxyConnectionError, match="within"):
        client.reload()


def test_timeout_covers_the_whole_exchange_not_each_read() -> None:
    directory = Path(tempfile.mkdtemp(prefix="hp-"))
    path = directory / "drip.sock"
    stop = threading.Event()
    # Listening before the client starts, so a refused connection cannot pass the test.
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen()

    def drip() -> None:
        connection, _ = server.accept()
        # Stops on its own after 2 s, so a client without an overall deadline gets a
        # complete answer and the test fails instead of hanging.
        give_up = time.monotonic() + 2
        with connection, server:
            while not stop.wait(0.02) and time.monotonic() < give_up:
                with contextlib.suppress(BrokenPipeError):
                    connection.sendall(b"x\n\n")

    thread = threading.Thread(target=drip, daemon=True)
    thread.start()
    client = HAProxyClient(admin_socket=str(path), command_timeout=0.2)
    started = time.monotonic()
    try:
        with pytest.raises(HAProxyConnectionError, match="no answer"):
            client.show_info()
        assert time.monotonic() - started < 1
    finally:
        stop.set()
        thread.join(timeout=5)
        shutil.rmtree(directory)


def test_missing_socket_raises_connection_error(tmp_path: Path) -> None:
    client = HAProxyClient(
        master_socket=str(tmp_path / "none"), admin_socket=str(tmp_path / "none")
    )
    with pytest.raises(HAProxyConnectionError):
        client.reload()
    with pytest.raises(HAProxyConnectionError):
        client.show_info()


# ---- reads ----


def test_show_info_and_show_stat_use_the_admin_socket(
    client: HAProxyClient, master: FakeSocket, admin: FakeSocket
) -> None:
    admin.replies = [
        b"Version: 3.4.7\nPid: 8\nUptime_sec: 1\n\n",
        b"# pxname,svname,type,\nweb,FRONTEND,0,\n\n",
    ]
    assert client.show_info().pid == 8
    assert client.show_stat()[0].proxy == "web"
    assert admin.requests == [b"show info\n", b"show stat\n"]
    assert master.requests == []


@pytest.mark.parametrize("reply", [b"Version: 3.4.7\nPid: 8\n", b"Permission denied\n\n"])
def test_show_info_rejects_a_bad_answer(
    client: HAProxyClient, admin: FakeSocket, reply: bytes
) -> None:
    admin.replies = [reply]
    with pytest.raises(HAProxyProtocolError):
        client.show_info()


def test_show_stat_rejects_output_cut_short(client: HAProxyClient, admin: FakeSocket) -> None:
    # Cut at a line end, so it would parse: the missing final empty line is the only sign.
    admin.replies = [b"# pxname,svname,type,\nweb,FRONTEND,0,\n"]
    with pytest.raises(HAProxyProtocolError, match="incomplete"):
        client.show_stat()


def test_empty_answer_raises(client: HAProxyClient, admin: FakeSocket) -> None:
    admin.replies = [b""]
    with pytest.raises(HAProxyProtocolError, match="without answering"):
        client.show_info()


def test_show_map_parses_entries(client: HAProxyClient, admin: FakeSocket) -> None:
    admin.replies = [b"0x55aeb a.example.com b1\n0x7fb73 b.example.com v 1\n0x7fb74 c\n\n"]
    assert client.show_map("hosts.map") == [
        MapEntry("a.example.com", "b1"),
        MapEntry("b.example.com", "v 1"),
        MapEntry("c", ""),
    ]
    assert admin.requests == [b"show map hosts.map\n"]


def test_show_map_of_an_empty_map(client: HAProxyClient, admin: FakeSocket) -> None:
    admin.replies = [b"\n"]
    assert client.show_map("hosts.map") == []


def test_show_map_keeps_a_line_separator_inside_a_value(
    client: HAProxyClient, admin: FakeSocket
) -> None:
    admin.replies = ["0x1 key a\u2028b\n\n".encode()]
    assert client.show_map("hosts.map") == [MapEntry("key", "a\u2028b")]


def test_show_map_of_an_unknown_map_raises(client: HAProxyClient, admin: FakeSocket) -> None:
    admin.replies = [b"Unknown map identifier. Please use #<id> or <file>.\n\n"]
    with pytest.raises(HAProxyCommandError, match="Unknown map"):
        client.show_map("nope.map")


# ---- map writes and escaping ----


def test_map_writes_send_the_documented_commands(client: HAProxyClient, admin: FakeSocket) -> None:
    client.add_map("hosts.map", "a.example.com", "b1")
    client.set_map("hosts.map", "a.example.com", "b2")
    client.del_map("hosts.map", "a.example.com")
    assert admin.requests == [
        b"add map hosts.map a.example.com b1\n",
        b"set map hosts.map a.example.com b2\n",
        b"del map hosts.map a.example.com\n",
    ]


@pytest.mark.parametrize(
    ("value", "wire"),
    [
        ("a;show info", rb"a\;show\ info"),
        ("two words", rb"two\ words"),
        ("back\\slash", rb"back\\slash"),
        ("end\\", rb"end\\"),
        ("<<", rb"\<\<"),
        ("\"q'", rb"\"q\'"),
        ("%[env(HOME)]", rb"\%\[env\(HOME\)\]"),
        ("${HOME}", rb"\$\{HOME\}"),
        ("#c", rb"\#c"),
        ("@1", rb"\@1"),
        ("café", "café".encode()),
    ],
)
def test_map_value_is_escaped_into_one_word(
    client: HAProxyClient, admin: FakeSocket, value: str, wire: bytes
) -> None:
    client.set_map("hosts.map", "key", value)
    assert admin.requests == [b"set map hosts.map key " + wire + b"\n"]


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "a\nshow info",
        "a\r",
        "a\x00b",
        "a\tb",
        "a\x7fb",
        "a\x85b",
        "lone\udc80surrogate",
    ],
)
def test_unsafe_values_are_rejected_before_connecting(
    client: HAProxyClient, admin: FakeSocket, bad: str
) -> None:
    for call in (
        lambda: client.add_map("hosts.map", "key", bad),
        lambda: client.set_map("hosts.map", "key", bad),
        lambda: client.add_map("hosts.map", bad, "value"),
        lambda: client.del_map("hosts.map", bad),
        lambda: client.add_map(bad, "key", "value"),
        lambda: client.show_map(bad),
        lambda: client.set_ssl_cert(bad, PEM),
        lambda: client.commit_ssl_cert(bad),
        lambda: client.abort_ssl_cert(bad),
    ):
        with pytest.raises(InvalidArgumentError) as caught:
            call()
        if bad:
            assert bad not in str(caught.value)
    assert admin.requests == []


@pytest.mark.parametrize(
    "key", ["two words", " leading", "trailing ", "#0x1", "#0x55e8636e2510", "#comment", "#"]
)
def test_map_key_with_a_space_or_a_leading_hash_is_rejected(
    client: HAProxyClient, admin: FakeSocket, key: str
) -> None:
    # A leading `#` makes `set map` and `del map` act on another entry by its reference.
    with pytest.raises(InvalidArgumentError):
        client.add_map("hosts.map", key, "v")
    with pytest.raises(InvalidArgumentError):
        client.set_map("hosts.map", key, "CHANGED")
    with pytest.raises(InvalidArgumentError):
        client.del_map("hosts.map", key)
    assert admin.requests == []


def test_hash_inside_a_map_key_is_allowed(client: HAProxyClient, admin: FakeSocket) -> None:
    client.set_map("hosts.map", "a#b", "v")
    assert admin.requests == [rb"set map hosts.map a\#b v" + b"\n"]


@pytest.mark.parametrize("name", ["#0", "@1", "a b"])
def test_map_and_cert_names_that_mean_something_else_are_rejected(
    client: HAProxyClient, admin: FakeSocket, name: str
) -> None:
    with pytest.raises(InvalidArgumentError):
        client.show_map(name)
    with pytest.raises(InvalidArgumentError):
        client.commit_ssl_cert(name)
    assert admin.requests == []


@pytest.mark.parametrize(
    "answer", [b"entry not found.\n\n", b"The command line is too big\n", b"\n\n"]
)
def test_map_write_with_any_other_answer_raises(
    client: HAProxyClient, admin: FakeSocket, answer: bytes
) -> None:
    admin.replies = [answer]
    with pytest.raises(HAProxyCommandError):
        client.set_map("hosts.map", "key", "value")


def test_map_write_with_no_answer_raises(client: HAProxyClient, admin: FakeSocket) -> None:
    admin.replies = [b""]
    with pytest.raises(HAProxyProtocolError):
        client.add_map("hosts.map", "key", "value")


# ---- certificates ----

SET_OK = b"Transaction created for certificate certs/a.pem!\n\n"
COMMIT_OK = b"Committing certs/a.pem.\nSuccess!\n\n"
ABORT_OK = b"Transaction aborted for certificate 'certs/a.pem'!\n\n"


def test_set_ssl_cert_sends_the_payload_format(client: HAProxyClient, admin: FakeSocket) -> None:
    admin.replies = [SET_OK]
    client.set_ssl_cert("certs/a.pem", PEM)
    assert admin.requests == [f"set ssl cert certs/a.pem <<\n{PEM_BODY}\n\n".encode()]


def test_set_ssl_cert_accepts_an_update_of_an_open_transaction(
    client: HAProxyClient, admin: FakeSocket
) -> None:
    admin.replies = [b"Transaction updated for certificate certs/a.pem!\n\n"]
    client.set_ssl_cert("certs/a.pem", PEM)


@pytest.mark.parametrize(
    "pem",
    [
        PEM.replace("-----\n-----BEGIN", "-----\n\n-----BEGIN"),
        PEM.replace("\n", "\r\n"),
        PEM.replace("-----\n-----BEGIN", "-----\n   \n-----BEGIN"),
        f"\n\n{PEM}\n\n",
    ],
)
def test_empty_lines_cannot_end_the_payload_early(
    client: HAProxyClient, admin: FakeSocket, pem: str
) -> None:
    admin.replies = [SET_OK]
    client.set_ssl_cert("certs/a.pem", pem)
    assert admin.requests == [f"set ssl cert certs/a.pem <<\n{PEM_BODY}\n\n".encode()]


def test_a_command_after_an_empty_line_stays_inside_the_payload(
    client: HAProxyClient, admin: FakeSocket
) -> None:
    admin.replies = [b"unable to load certificate\n\n"]
    with pytest.raises(HAProxyCommandError):
        client.set_ssl_cert("certs/a.pem", f"{PEM}\nabort ssl cert certs/a.pem\n")
    assert admin.requests[0].count(b"\n\n") == 1
    assert admin.requests[0].endswith(b"abort ssl cert certs/a.pem\n\n")


@pytest.mark.parametrize(
    "pem", ["", "\n \n", "a\x00b", "a\x1bb", "a\rb", "a\x85b", "lone\udc80surrogate"]
)
def test_bad_pem_is_rejected_before_connecting(
    client: HAProxyClient, admin: FakeSocket, pem: str
) -> None:
    with pytest.raises(InvalidArgumentError):
        client.set_ssl_cert("certs/a.pem", pem)
    assert admin.requests == []


def test_replace_ssl_cert_sets_then_commits(client: HAProxyClient, admin: FakeSocket) -> None:
    admin.replies = [SET_OK, COMMIT_OK]
    client.replace_ssl_cert("certs/a.pem", PEM)
    assert admin.requests[1] == b"commit ssl cert certs/a.pem\n"
    assert len(admin.requests) == 2


SET = b"set ssl cert certs/a.pem <<"
COMMIT = b"commit ssl cert certs/a.pem"
ABORT = b"abort ssl cert certs/a.pem"
NO_TRANSACTION = b"No ongoing transaction!\n\n"


@pytest.mark.parametrize(
    ("name", "pem", "replies", "error", "match", "sent"),
    [
        (
            "certs/a.pem",
            PEM,
            [b"Can't update certs/a.pem!\n\n", NO_TRANSACTION],
            HAProxyCommandError,
            "set ssl cert failed",
            [SET, ABORT],
        ),
        # HAProxy may have opened the transaction before the client gave up on the answer.
        ("certs/a.pem", PEM, [SET_OK, ABORT_OK], HAProxyConnectionError, None, [SET, ABORT]),
        ("certs/a.pem", PEM, [b"", ABORT_OK], HAProxyProtocolError, None, [SET, ABORT]),
        (
            "certs/a.pem",
            PEM,
            [SET_OK, b"inconsistencies\nCan't commit certs/a.pem!\n\n", ABORT_OK],
            HAProxyCommandError,
            "inconsistencies",
            [SET, COMMIT, ABORT],
        ),
        (
            "certs/a.pem",
            PEM,
            [SET_OK, b"Failed!\n\n", NO_TRANSACTION],
            HAProxyCommandError,
            "commit ssl cert failed",
            [SET, COMMIT, ABORT],
        ),
        # Nothing was sent, and an abort could drop a transaction that another caller opened.
        ("#0", PEM, [], InvalidArgumentError, None, []),
        ("certs/a.pem", "a\x00b", [], InvalidArgumentError, None, []),
    ],
    ids=[
        "failed-set",
        "set-without-answer",
        "empty-answer-to-set",
        "failed-commit",
        "abort-fails-too",
        "invalid-name",
        "invalid-pem",
    ],
)
def test_replace_ssl_cert_aborts_after_any_failure_and_raises_the_first_error(
    sockets: tuple[FakeSocket, FakeSocket],
    name: str,
    pem: str,
    replies: list[bytes],
    error: type[Exception],
    match: str | None,
    sent: list[bytes],
) -> None:
    master, admin = sockets
    client = HAProxyClient(
        master_socket=str(master.path), admin_socket=str(admin.path), command_timeout=0.5
    )
    if error is HAProxyConnectionError:
        admin.delays = [2.0]
    admin.replies = replies
    with pytest.raises(error, match=match):
        client.replace_ssl_cert(name, pem)
    assert [request.split(b"\n")[0] for request in admin.requests] == sent


@pytest.mark.parametrize("answer", [b"Committing a.\nFailed!\n\n", b"No ongoing transaction!\n\n"])
def test_commit_needs_success_in_its_answer(
    client: HAProxyClient, admin: FakeSocket, answer: bytes
) -> None:
    admin.replies = [answer]
    with pytest.raises(HAProxyCommandError):
        client.commit_ssl_cert("certs/a.pem")


def test_abort_without_a_transaction_raises(client: HAProxyClient, admin: FakeSocket) -> None:
    admin.replies = [b"No ongoing transaction!\n\n"]
    with pytest.raises(HAProxyCommandError):
        client.abort_ssl_cert("certs/a.pem")


# ---- secrets ----


def _all_exception_text(exc: BaseException) -> str:
    """Return the text of an exception and of every exception chained to it."""
    parts = []
    current: BaseException | None = exc
    while current is not None:
        parts.append(f"{current!s} {current!r}")
        current = current.__cause__ or current.__context__
    return " ".join(parts)


def test_pem_never_reaches_errors_or_logs(
    client: HAProxyClient, admin: FakeSocket, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    admin.replies = [b"unable to load certificate\nCan't update certs/a.pem!\n\n"]
    with pytest.raises(HAProxyError) as caught:
        client.set_ssl_cert("certs/a.pem", PEM)
    assert PAYLOAD_MARKER not in _all_exception_text(caught.value)
    assert PAYLOAD_MARKER not in caplog.text


def test_pem_is_not_in_an_error_when_the_socket_fails(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    gone = HAProxyClient(admin_socket=str(tmp_path / "none"))
    with pytest.raises(HAProxyConnectionError) as caught:
        gone.set_ssl_cert("certs/a.pem", PEM)
    assert PAYLOAD_MARKER not in _all_exception_text(caught.value)
    assert PAYLOAD_MARKER not in caplog.text


def test_registered_secret_is_redacted_from_haproxy_text(
    client: HAProxyClient, master: FakeSocket, admin: FakeSocket
) -> None:
    sensitive = "haproxy-client-test-secret-7f3a"
    register_secret(sensitive)

    master.replies = [f"Success=0\n--\n[ALERT] line: {sensitive}\n\n".encode()]
    result = client.reload()
    assert sensitive not in result.startup_log
    assert REDACTED in result.startup_log

    master.replies = [f"garbage {sensitive}\n".encode()]
    with pytest.raises(HAProxyProtocolError) as protocol_error:
        client.reload()
    assert sensitive not in str(protocol_error.value)

    admin.replies = [f"refused {sensitive}\n\n".encode()]
    with pytest.raises(HAProxyCommandError) as command_error:
        client.set_map("hosts.map", "key", "value")
    assert sensitive not in str(command_error.value)


def test_a_short_registered_secret_never_breaks_parsing(
    client: HAProxyClient, master: FakeSocket, admin: FakeSocket
) -> None:
    """A short secret can sit inside `Success=1` or a pid. Answers are parsed as HAProxy sent
    them, and only the text that is shown is redacted."""
    register_secret("ess", "123")

    master.replies = [b"Success=1\n--\n[NOTICE] loaded 123 files\n\n"]
    assert client.reload() == ReloadResult(
        success=True, startup_log=f"[NOTICE] loaded {REDACTED} files"
    )
    admin.replies = [b"Version: 3.4.7\nPid: 1234\nUptime_sec: 1\n\n"]
    assert client.show_info().pid == 1234
    admin.replies = [b"0x1 key 123\n\n"]
    assert client.show_map("hosts.map") == [MapEntry("key", "123")]
