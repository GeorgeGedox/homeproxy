"""The client against a real HAProxy, built from the same image as the product.

Each test gets its own container (`live`, see `tests/integration/haproxy_live.py`), except the map
attack cases, which share one per module.
"""

import json
import socket
import subprocess
import threading
from collections.abc import Callable, Iterator

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
from homeproxy.haproxy.stats import ProxyType, StatRow
from tests.integration.haproxy_live import (
    CONFIG,
    HOST_A,
    HOSTS_MAP,
    TRUSTED_LIST,
    TRUSTED_RANGE,
    LiveHAProxy,
    PemPair,
    docker,
    started,
    wait_for_haproxy,
)

CERT_NAME = "certs/site.pem"


@pytest.fixture(scope="module")
def shared_live(certs: tuple[PemPair, PemPair]) -> Iterator[LiveHAProxy]:
    """One HAProxy for the module, for `map_live`."""
    with started(certs) as instance:
        yield instance


@pytest.fixture
def map_live(shared_live: LiveHAProxy) -> Iterator[LiveHAProxy]:
    """The shared HAProxy, for tests that only change the hosts map at runtime. The map is put
    back as the release file has it after each test."""
    yield shared_live
    raw_runtime(shared_live, f"clear map {HOSTS_MAP}")
    shared_live.client.add_map(HOSTS_MAP, HOST_A, "first")


# ---- reload ----


def test_reload_success_reports_its_log_and_starts_a_new_worker(live: LiveHAProxy) -> None:
    worker = live.client.show_info().pid

    result = live.client.reload()

    assert result.success is True
    assert "Loading success" in result.startup_log
    # With no sleep: `reload` returns only once the new worker answers the admin socket.
    assert live.client.show_info().pid != worker


def test_reload_serves_the_release_behind_the_symlink(
    live: LiveHAProxy, certs: tuple[PemPair, PemPair]
) -> None:
    live.publish_release(CONFIG.format(marker="v2"), certs[0])

    assert live.client.reload().success is True

    assert live.get() == "first v2"


def test_failed_reload_returns_success_0_with_its_startup_log(
    live: LiveHAProxy, certs: tuple[PemPair, PemPair]
) -> None:
    worker = live.client.show_info().pid
    live.publish_release(CONFIG.format(marker="v2") + "\nbogus_keyword here\n", certs[0])

    result = live.client.reload()

    assert result.success is False
    assert "unknown keyword 'bogus_keyword'" in result.startup_log
    assert "[ALERT]" in result.startup_log
    # The old worker keeps serving the old config, on the same runtime socket.
    assert live.client.show_info().pid == worker
    assert live.get() == "first v1"


def test_failed_reload_of_a_config_that_passes_the_check(
    live: LiveHAProxy, certs: tuple[PemPair, PemPair]
) -> None:
    # `haproxy -c` does not bind, so only the reload itself finds this one.
    config = CONFIG.format(marker="v2").replace("bind :8080", "bind :8080\n    bind 192.0.2.1:8081")
    live.publish_release(config, certs[0])
    # Raises if the check fails.
    docker("exec", live.container, "haproxy", "-c", "-f", "/data/generated/conf.d/")

    result = live.client.reload()

    assert result.success is False
    assert "cannot bind socket" in result.startup_log
    assert live.get() == "first v1"


def test_rollback_after_a_failed_reload_works_at_once(
    live: LiveHAProxy, certs: tuple[PemPair, PemPair]
) -> None:
    live.publish_release(CONFIG.format(marker="v2") + "\nbogus_keyword here\n", certs[0])
    assert live.client.reload().success is False

    live.publish_release(CONFIG.format(marker="v3"), certs[0])
    result = live.client.reload()

    assert result.success is True
    assert live.get() == "first v3"
    assert live.client.reload().success is True


def test_reload_timeout_is_an_unknown_result_and_haproxy_finishes(live: LiveHAProxy) -> None:
    worker = live.client.show_info().pid
    impatient = HAProxyClient(
        master_socket=live.client.master_socket,
        admin_socket=live.client.admin_socket,
        # Long enough to send the command on a busy host, too short for the reload.
        reload_timeout=0.05,
    )

    with pytest.raises(HAProxyConnectionError):
        impatient.reload()

    wait_for_haproxy(
        live.container, lambda: live.client.show_info().pid != worker, "the reload to finish"
    )
    assert live.get() == "first v1"


def test_old_worker_with_an_open_connection_does_not_block_the_reload(live: LiveHAProxy) -> None:
    with socket.create_connection(("127.0.0.1", live.http_port), timeout=5.0):
        # This connection stays open through the reload, like a WebSocket would.
        assert live.client.reload().success is True
        assert live.get() == "first v1"


def test_overlapping_reloads_end_cleanly_and_leave_a_worker(live: LiveHAProxy) -> None:
    outcomes: list[object] = []

    def reload_once() -> None:
        try:
            outcomes.append(live.client.reload())
        except HAProxyError as exc:
            outcomes.append(exc)

    threads = [threading.Thread(target=reload_once) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=90)

    # Which ones win is a race. None may hang, and the socket refuses the others.
    assert len(outcomes) == len(threads)
    assert any(isinstance(outcome, ReloadResult) and outcome.success for outcome in outcomes)
    assert all(
        isinstance(outcome, ReloadResult | HAProxyConnectionError | HAProxyProtocolError)
        for outcome in outcomes
    )
    wait_for_haproxy(live.container, lambda: live.get() == "first v1", "HAProxy to serve again")


def test_runtime_socket_answers_throughout_a_reload(live: LiveHAProxy) -> None:
    failures: list[HAProxyError] = []
    stop = threading.Event()

    def poll() -> None:
        while not stop.is_set():
            try:
                live.client.show_stat()
            except HAProxyError as exc:
                failures.append(exc)

    poller = threading.Thread(target=poll)
    poller.start()
    try:
        for _ in range(5):
            assert live.client.reload().success is True
    finally:
        stop.set()
        poller.join(timeout=30)
    assert failures == []


# ---- show info and show stat ----


def test_show_info_reports_the_running_version(live: LiveHAProxy) -> None:
    info = live.client.show_info()
    assert info.version.startswith("3.4.")
    assert info.pid > 0
    assert info.uptime_seconds >= 0


def rows_by_name(rows: list[StatRow]) -> dict[tuple[str, str], StatRow]:
    """Index `show stat` rows by proxy and service name."""
    return {(row.proxy, row.service): row for row in rows}


def test_show_stat_reports_status_and_counters(live: LiveHAProxy) -> None:
    def checks_settled() -> bool:
        rows = rows_by_name(live.client.show_stat())
        return rows["pool", "alive"].status == "UP" and rows["pool", "dead"].status == "DOWN"

    wait_for_haproxy(live.container, checks_settled, "health checks to settle")
    before = rows_by_name(live.client.show_stat())["web", "FRONTEND"].requests_total
    assert before is not None
    live.get()

    rows = rows_by_name(live.client.show_stat())

    frontend = rows["web", "FRONTEND"]
    assert frontend.type is ProxyType.FRONTEND
    assert frontend.status == "OPEN"
    assert frontend.requests_total is not None
    assert frontend.requests_total > before
    assert rows["pool", "BACKEND"].type is ProxyType.BACKEND
    assert rows["pool", "BACKEND"].status == "UP"
    assert rows["pool", "alive"].type is ProxyType.SERVER
    # HAProxy marks a check that is running with "* ".
    assert rows["pool", "dead"].check_status.lstrip("* ") == "L4CON"


# ---- runtime maps ----


def test_map_changes_apply_to_traffic_without_a_reload(live: LiveHAProxy) -> None:
    worker = live.client.show_info().pid
    other = "b.example.com"
    assert live.get(other) == "none v1"

    live.client.add_map(HOSTS_MAP, other, "added")
    assert live.get(other) == "added v1"
    assert MapEntry(other, "added") in live.client.show_map(HOSTS_MAP)

    live.client.set_map(HOSTS_MAP, other, "changed")
    assert live.get(other) == "changed v1"

    live.client.del_map(HOSTS_MAP, other)
    assert live.get(other) == "none v1"
    assert live.client.show_map(HOSTS_MAP) == [MapEntry(HOST_A, "first")]

    assert live.client.show_info().pid == worker


def test_runtime_map_change_is_lost_at_reload_unless_the_file_has_it(live: LiveHAProxy) -> None:
    live.client.add_map(HOSTS_MAP, "b.example.com", "added")
    assert live.client.reload().success is True
    assert live.get("b.example.com") == "none v1"


def test_map_refuses_what_does_not_exist(live: LiveHAProxy) -> None:
    with pytest.raises(HAProxyCommandError, match="entry not found"):
        live.client.set_map(HOSTS_MAP, "missing.example.com", "x")
    with pytest.raises(HAProxyCommandError, match="Key not found"):
        live.client.del_map(HOSTS_MAP, "missing.example.com")
    with pytest.raises(HAProxyCommandError, match="Unknown map"):
        live.client.add_map("other.map", "k", "v")
    with pytest.raises(HAProxyCommandError, match="Unknown map"):
        live.client.show_map("other.map")
    # The name in the config is the only one that works, not the file's full path.
    with pytest.raises(HAProxyCommandError, match="Unknown map"):
        live.client.show_map(f"/data/generated/{HOSTS_MAP}")


def test_add_map_on_an_existing_key_keeps_both_and_del_removes_one(live: LiveHAProxy) -> None:
    live.client.add_map(HOSTS_MAP, "dup", "one")
    live.client.add_map(HOSTS_MAP, "dup", "two")
    assert [e.value for e in live.client.show_map(HOSTS_MAP) if e.key == "dup"] == ["one", "two"]

    live.client.del_map(HOSTS_MAP, "dup")
    assert [e.value for e in live.client.show_map(HOSTS_MAP) if e.key == "dup"] == ["two"]


ATTACK_VALUES = [
    "x;del map hosts.map a.example.com",
    "x; show info",
    "x <<",
    "<<",
    "<<EOF",
    "two  spaces",
    " leading",
    "trailing ",
    '"double"',
    "'single'",
    "back\\slash",
    "trailing\\",
    "\\;",
    "%[env(HOME)]",
    "${HOME}",
    "#comment",
    "@1",
    "`id`",
    "$(id)",
    "a|b&c>d",
    "100%",
    "café",
]


@pytest.mark.parametrize("value", ATTACK_VALUES)
def test_attack_value_is_one_literal_map_value(map_live: LiveHAProxy, value: str) -> None:
    map_live.client.add_map(HOSTS_MAP, "attack", value)

    assert map_live.client.show_map(HOSTS_MAP) == [
        MapEntry(HOST_A, "first"),
        MapEntry("attack", value),
    ]


def raw_runtime(live: LiveHAProxy, command: str) -> str:
    """Send one command to the runtime API without the client, to see HAProxy's raw answer."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(10.0)
        connection.connect(live.client.admin_socket)
        connection.sendall(f"{command}\n".encode())
        chunks = []
        while chunk := connection.recv(65536):
            chunks.append(chunk)
    return b"".join(chunks).decode()


def test_key_that_names_another_entry_by_reference_is_refused(map_live: LiveHAProxy) -> None:
    # `show map` starts each line with the entry's reference, and `set map` and `del map`
    # accept `#<reference>` in place of a key, even with the `#` escaped.
    reference = raw_runtime(map_live, f"show map {HOSTS_MAP}").split(" ", 1)[0]
    assert reference.startswith("0x")
    key = f"#{reference}"

    with pytest.raises(InvalidArgumentError):
        map_live.client.set_map(HOSTS_MAP, key, "CHANGED")
    with pytest.raises(InvalidArgumentError):
        map_live.client.del_map(HOSTS_MAP, key)
    with pytest.raises(InvalidArgumentError):
        map_live.client.add_map(HOSTS_MAP, key, "value")

    assert map_live.client.show_map(HOSTS_MAP) == [MapEntry(HOST_A, "first")]
    # What the rule prevents: the escaped form still reaches the other entry.
    assert raw_runtime(map_live, f"set map {HOSTS_MAP} \\{key} CHANGED") == "\n"
    assert map_live.client.show_map(HOSTS_MAP) == [MapEntry(HOST_A, "CHANGED")]


# A leading `#` is refused in keys, as tested above.
ATTACK_KEYS = [value for value in ATTACK_VALUES if " " not in value and not value.startswith("#")]


@pytest.mark.parametrize("key", ATTACK_KEYS)
def test_attack_key_is_one_literal_map_key(map_live: LiveHAProxy, key: str) -> None:
    map_live.client.add_map(HOSTS_MAP, key, "value")
    assert map_live.client.show_map(HOSTS_MAP) == [
        MapEntry(HOST_A, "first"),
        MapEntry(key, "value"),
    ]

    map_live.client.del_map(HOSTS_MAP, key)
    assert map_live.client.show_map(HOSTS_MAP) == [MapEntry(HOST_A, "first")]


def acl_patterns(live: LiveHAProxy) -> list[str]:
    """Return the patterns of the trusted list that lookups use now."""
    answer = raw_runtime(live, f"show acl {TRUSTED_LIST}")
    return [line.split(" ", 1)[1] for line in answer.splitlines() if line]


def test_replace_acl_swaps_every_pattern_without_a_reload(live: LiveHAProxy) -> None:
    worker = live.client.show_info().pid

    live.client.replace_acl(TRUSTED_LIST, ["192.0.2.0/24", "2001:db8::/32"])

    assert acl_patterns(live) == ["192.0.2.0/24", "2001:db8::/32"]
    assert live.client.show_info().pid == worker


@pytest.mark.parametrize(
    ("patterns", "error"),
    [(["192.0.2.0/24", "999.1.1.1"], HAProxyCommandError), ([], InvalidArgumentError)],
    ids=["refused-pattern", "empty"],
)
def test_replace_acl_that_fails_keeps_the_old_patterns(
    live: LiveHAProxy, patterns: list[str], error: type[HAProxyError]
) -> None:
    """A list that is empty, or only partly new, would trust the wrong sources."""
    with pytest.raises(error):
        live.client.replace_acl(TRUSTED_LIST, patterns)

    assert acl_patterns(live) == [TRUSTED_RANGE]


# ---- certificates ----


def test_replace_ssl_cert_hot_swaps_without_a_reload(
    live: LiveHAProxy, certs: tuple[PemPair, PemPair]
) -> None:
    first, second = certs
    worker = live.client.show_info().pid
    assert live.served_cert() == first.der

    live.client.replace_ssl_cert(CERT_NAME, second.bundle)

    assert live.served_cert() == second.der
    assert live.client.show_info().pid == worker


def test_empty_lines_in_a_bundle_do_not_cut_off_the_key(
    live: LiveHAProxy, certs: tuple[PemPair, PemPair]
) -> None:
    second = certs[1]
    live.client.replace_ssl_cert(CERT_NAME, f"{second.cert}\n\n{second.key}\n")
    assert live.served_cert() == second.der


def test_set_then_abort_changes_nothing(live: LiveHAProxy, certs: tuple[PemPair, PemPair]) -> None:
    first, second = certs
    live.client.set_ssl_cert(CERT_NAME, second.bundle)
    assert live.served_cert() == first.der

    live.client.abort_ssl_cert(CERT_NAME)

    assert live.served_cert() == first.der
    with pytest.raises(HAProxyCommandError, match="No ongoing transaction"):
        live.client.commit_ssl_cert(CERT_NAME)


def test_mismatched_key_fails_and_leaves_the_old_cert_and_no_transaction(
    live: LiveHAProxy, certs: tuple[PemPair, PemPair]
) -> None:
    first, second = certs
    with pytest.raises(HAProxyCommandError, match="inconsistencies"):
        live.client.replace_ssl_cert(CERT_NAME, second.cert + first.key)

    assert live.served_cert() == first.der
    with pytest.raises(HAProxyCommandError, match="No ongoing transaction"):
        live.client.abort_ssl_cert(CERT_NAME)
    live.client.replace_ssl_cert(CERT_NAME, second.bundle)
    assert live.served_cert() == second.der


def test_bundle_without_a_key_fails_and_leaves_the_old_cert(
    live: LiveHAProxy, certs: tuple[PemPair, PemPair]
) -> None:
    first, second = certs
    with pytest.raises(HAProxyCommandError):
        live.client.replace_ssl_cert(CERT_NAME, second.cert)

    assert live.served_cert() == first.der
    live.client.replace_ssl_cert(CERT_NAME, second.bundle)
    assert live.served_cert() == second.der


def test_certificate_that_is_not_loaded_is_refused(
    live: LiveHAProxy, certs: tuple[PemPair, PemPair]
) -> None:
    with pytest.raises(HAProxyCommandError, match="not referenced"):
        live.client.replace_ssl_cert("certs/other.pem", certs[1].bundle)


def test_invalid_pem_error_does_not_repeat_the_key(
    live: LiveHAProxy, certs: tuple[PemPair, PemPair]
) -> None:
    key_lines = certs[1].key.splitlines()[1:-1]
    broken = f"-----BEGIN PRIVATE KEY-----\n{key_lines[0][:-1]}!\n" + "\n".join(key_lines[1:])

    with pytest.raises(HAProxyCommandError) as caught:
        live.client.set_ssl_cert(CERT_NAME, broken)

    message = str(caught.value)
    assert "set ssl cert failed" in message
    assert not any(line in message for line in key_lines)


# ---- the built image ----

IMAGE_SCRIPT = """
import json
from homeproxy.haproxy.client import HAProxyClient
client = HAProxyClient()
before = client.show_info().pid
result = client.reload()
print(json.dumps({
    "success": result.success,
    "changed": client.show_info().pid != before,
    "version": client.show_info().version,
    "proxies": [row.proxy for row in client.show_stat()],
}))
"""


def test_client_works_with_the_default_sockets_in_the_built_image(
    stack: object, test_compose: Callable[..., subprocess.CompletedProcess[str]]
) -> None:
    """Prove socket paths and permissions as the API's user, in the image we ship."""
    done = test_compose(
        "exec",
        "-T",
        "--user",
        "homeproxy",
        "homeproxy",
        "/opt/homeproxy/venv/bin/python",
        "-c",
        IMAGE_SCRIPT,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    answer = json.loads(done.stdout)
    assert answer["success"] is True
    assert answer["changed"] is True
    assert answer["version"].startswith("3.4.")
    assert "placeholder" in answer["proxies"]
