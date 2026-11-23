"""A real HAProxy running generated config, with an upstream inside it and a log collector in
this process. Shared by the golden behavior tests and their fixtures in `conftest.py`."""

import asyncio
import base64
import http.client
import os
import re
import socket
import ssl
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any

from ruamel.yaml import YAML

from homeproxy.generator import render, write_release
from homeproxy.logs import SYSLOG_SOCKET, AccessLog, LogCollector
from homeproxy.models import Config, HttpRoute, Route, parse_route
from homeproxy.store.basic_auth import hash_basic_auth_password
from homeproxy.util.run import run

GOLDEN = Path(__file__).parent
HTTP_PORT = 8080
HTTPS_PORT = 8443
TCP_PORT = 9100
UPSTREAM = "127.0.0.1:9000"
OTHER_PORT_UPSTREAM = "127.0.0.1:9001"
OTHER_ADDRESS_UPSTREAM = "127.0.0.2:9000"
TLS_UPSTREAM = "127.0.0.1:9443"
UPSTREAM_NAME = "upstream-host"
HOME_CANARY = "home-value-must-not-appear"
# TEST-NET-3, so the test client is never inside this LAN.
NOT_THE_CLIENT = "203.0.113.0/24"

# HAProxy splits the credentials at the first colon, so a password may hold one.
LOCKED_PASSWORD = "right:password"  # noqa: S105 - a test credential
BASIC_AUTH = {"locked": {"alice": hash_basic_auth_password(LOCKED_PASSWORD)}}

# A host that a hand edit or a runtime update put in hosts.map, with no entry in lan-only.map.
MISMATCHED_HOST = "stray.example.com"
# Some upstreams read `_` in a header name as `-`, so the underscore spellings are spoofable too.
UNDERSCORE_HEADERS = (
    "X_Forwarded_For",
    "X_Forwarded_Host",
    "X_Forwarded_Port",
    "X_Forwarded_Proto",
    "X_Real_IP",
    "CF_Connecting_IP",
)
SPOOFABLE_HEADERS = (
    "X-Forwarded-Host",
    "X-Forwarded-Port",
    "X-Real-IP",
    "Forwarded",
    *UNDERSCORE_HEADERS,
)
# The upstream answers with the address it was reached on and the forwarding headers it got.
ECHO = "|".join(
    [
        "%[dst]:%[dst_port]",
        "%[req.fhdr_cnt(x-forwarded-for)]",
        "%[req.fhdr(x-forwarded-for)]",
        "%[req.hdr(x-forwarded-proto)]",
        *(f"%[req.fhdr_cnt({name.lower()})]" for name in SPOOFABLE_HEADERS),
    ]
)
LOG_FORMATS_PATH = "/haproxy-log-formats"
LOG_FORMATS_RULE = (
    "http-request return status 200 content-type text/plain"
    ' string "${HAPROXY_HTTP_LOG_FMT}|${HAPROXY_TCP_LOG_FMT}"'
    f" if {{ path {LOG_FORMATS_PATH} }}"
)
REQUEST_HEADERS_PATH = "/request-headers"
REQUEST_HEADERS_RULE = (
    'http-request return status 200 content-type text/plain lf-string "%[req.hdrs]"'
    f" if {{ path {REQUEST_HEADERS_PATH} }}"
)
TEST_UPSTREAM = f"""
frontend test_upstream
    bind {UPSTREAM}
    bind {OTHER_PORT_UPSTREAM}
    bind {OTHER_ADDRESS_UPSTREAM}
    bind {TLS_UPSTREAM} ssl crt /data/certs/default.pem
    # HAProxy's own default log formats, which it puts in its environment.
    {LOG_FORMATS_RULE}
    {REQUEST_HEADERS_RULE}
    http-request return status 200 content-type text/plain hdr X-Path "%[pathq]" lf-string "{ECHO}"
"""

PASSTHROUGH_HOST = "pass.example.com"


def load_yaml(path: Path) -> Any:
    return YAML(typ="rt").load(path)


def plain_route(name: str, exposure: str = "public", **fields: Any) -> HttpRoute:
    route = parse_route(
        {
            "version": 1,
            "name": name,
            "type": "http",
            "hosts": [f"{name.strip('-')}.example.com"],
            "exposure": exposure,
            "upstreams": [{"address": UPSTREAM}],
            **fields,
        }
    )
    assert isinstance(route, HttpRoute)
    return route


def case_route(case: str, name: str, **fields: Any) -> HttpRoute:
    """Load a golden case's route, pointed at the test upstream."""
    data = {
        **load_yaml(GOLDEN / case / "input" / "routes" / f"{name}.yaml"),
        "upstreams": [{"address": UPSTREAM}],
        **fields,
    }
    route = parse_route(data)
    assert isinstance(route, HttpRoute)
    return route


def make_config(lan_cidr: str, **changes: Any) -> Config:
    """Return the base config with this LAN and the test ports, and `changes` on top."""
    data = load_yaml(GOLDEN / "base-config.yaml")
    data["network"].update(lan_cidrs=[lan_cidr], http_port=HTTP_PORT, https_port=HTTPS_PORT)
    return Config.model_validate({**data, **changes})


ROUTES: tuple[Route, ...] = (
    plain_route("app"),
    plain_route("private", exposure="lan"),
    plain_route("own-hsts", response_headers={"Strict-Transport-Security": "max-age=300"}),
    plain_route("global"),
    plain_route("trailing-"),
    plain_route("locked", auth={"type": "basic"}),
    plain_route(
        "tls-unverified",
        upstreams=[{"address": TLS_UPSTREAM}],
        tls_upstream={"enabled": True, "verify": False},
    ),
    plain_route(
        "tls-verified",
        upstreams=[{"address": TLS_UPSTREAM}],
        tls_upstream={"enabled": True, "verify": True},
    ),
    case_route("attack-header-values", "attack"),
    case_route(
        "attack-health-paths", "encoded-newline", health_check={"path": "/%0a", "interval": "100ms"}
    ),
    case_route(
        "attack-health-paths", "encoded-nul", health_check={"path": "/%00", "interval": "100ms"}
    ),
    # The SNI layer owns the HTTPS port, so every test above also runs through it.
    parse_route(
        {
            "version": 1,
            "name": "pass",
            "type": "passthrough",
            "hosts": [PASSTHROUGH_HOST],
            "exposure": "public",
            "upstreams": [{"address": TLS_UPSTREAM}],
        }
    ),
    parse_route(
        {
            "version": 1,
            "name": "game",
            "type": "tcp",
            "listen_port": TCP_PORT,
            "exposure": "public",
            "upstreams": [{"address": UPSTREAM}],
        }
    ),
)


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict[str, str]
    body: str


class CollectorThread:
    """A `LogCollector` in its own event loop thread, as the API process runs it."""

    def __init__(self, socket_path: Path) -> None:
        self.collector = LogCollector(socket_path)
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)

    def __enter__(self) -> "CollectorThread":
        self._thread.start()
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.stop()
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=10)
        self._loop.close()

    def start(self) -> None:
        asyncio.run_coroutine_threadsafe(self.collector.start(), self._loop).result(timeout=10)

    def stop(self) -> None:
        self._call(self.collector.close)

    def entries(self) -> list[AccessLog]:
        return self._call(self.collector.tail)

    def _call[T](self, function: Callable[[], T]) -> T:
        async def call() -> T:
            return function()

        return asyncio.run_coroutine_threadsafe(call(), self._loop).result(timeout=10)


@dataclass(frozen=True)
class Proxy:
    container: str
    http_port: int
    https_port: int
    tcp_port: int
    collector: CollectorThread

    def logs(self) -> str:
        result = run(["docker", "logs", self.container])
        return result.stdout + result.stderr

    def get(
        self, *hosts: str, path: str = "/", extra: tuple[tuple[str, str], ...] = ()
    ) -> Response:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return request(
            http.client.HTTPSConnection("127.0.0.1", self.https_port, timeout=10, context=context),
            hosts,
            path,
            extra,
        )

    def get_plain(
        self, *hosts: str, path: str = "/", extra: tuple[tuple[str, str], ...] = ()
    ) -> Response:
        connection = http.client.HTTPConnection("127.0.0.1", self.http_port, timeout=10)
        return request(connection, hosts, path, extra)

    def runtime(self, command: str) -> None:
        """Send one command to the runtime API."""
        script = f'echo "{command}" | socat - /run/haproxy/admin.sock'
        run(["docker", "exec", self.container, "sh", "-c", script])

    def entries(self, frontend: str) -> list[AccessLog]:
        return [entry for entry in self.collector.entries() if entry.frontend == frontend]


def tls_request(port: int, sni: str, host: str, path: str) -> str:
    """Send one request over TLS with any SNI, which `http.client` cannot, and return the reply."""
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with (
        socket.create_connection(("127.0.0.1", port), timeout=10) as raw,
        context.wrap_socket(raw, server_hostname=sni) as tls,
    ):
        request = f"GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n"
        tls.sendall(request.encode())
        reply = b""
        while chunk := tls.recv(4096):
            reply += chunk
    return reply.decode(errors="replace")


def request(
    connection: http.client.HTTPConnection,
    hosts: tuple[str, ...],
    path: str,
    extra: tuple[tuple[str, str], ...],
) -> Response:
    try:
        connection.putrequest("GET", path, skip_host=True, skip_accept_encoding=True)
        for host in hosts:
            connection.putheader("Host", host)
        for name, value in extra:
            connection.putheader(name, value)
        connection.endheaders()
        response = connection.getresponse()
        headers = {name.lower(): value for name, value in response.getheaders()}
        return Response(response.status, headers, response.read().decode())
    finally:
        connection.close()


def published_port(container: str, port: int) -> int:
    output = run(["docker", "port", container, f"{port}/tcp"]).stdout
    return int(output.splitlines()[0].rpartition(":")[2])


def write_test_release(
    data_dir: Path, name: str, config: Config, routes: tuple[Route, ...]
) -> None:
    """Write a release with the test upstream, and point `/data/generated` at it."""
    release = data_dir / "releases" / name
    write_release(render(config, routes, basic_auth=BASIC_AUTH), release)
    (release / "conf.d" / "90-test-upstream.cfg").write_text(TEST_UPSTREAM)
    with (release / "hosts.map").open("a") as hosts_map:
        hosts_map.write(f"{MISMATCHED_HOST} route-private\n")
    link = data_dir / "generated.new"
    link.symlink_to(f"releases/{name}")
    link.replace(data_dir / "generated")


@contextmanager
def running_proxy(
    data_dir: Path,
    lan_cidr: str,
    image: str,
    routes: tuple[Route, ...] = ROUTES,
    config_changes: dict[str, Any] | None = None,
) -> Iterator[Proxy]:
    """Render `routes` into `data_dir` and run them in HAProxy with the image's arguments.

    The name `upstream-host` resolves to 127.0.0.1. `config_changes` replace top-level sections
    of the base config.
    """
    config = make_config(lan_cidr, **(config_changes or {}))
    write_test_release(data_dir, "first", config, routes)
    run_dir = data_dir / "run"
    run_dir.mkdir()
    with CollectorThread(run_dir / Path(SYSLOG_SOCKET).name) as collector:
        yield from running_container(data_dir, run_dir, image, collector)


def running_container(
    data_dir: Path, run_dir: Path, image: str, collector: CollectorThread
) -> Iterator[Proxy]:
    container = run(
        [
            "docker",
            "run",
            "--detach",
            # Gone when it stops, and `tests/conftest.py` removes what a killed run left running.
            "--rm",
            f"--label=homeproxy.golden={os.environ.get('COMPOSE_PROJECT_NAME', '')}",
            f"--user={os.getuid()}:{os.getgid()}",
            f"--tmpfs=/run/haproxy:uid={os.getuid()}",
            f"--volume={data_dir}:/data",
            f"--volume={run_dir}:{Path(SYSLOG_SOCKET).parent}",
            f"--env=HOME={HOME_CANARY}",
            f"--add-host={UPSTREAM_NAME}:127.0.0.1",
            f"--publish=127.0.0.1::{HTTP_PORT}",
            f"--publish=127.0.0.1::{HTTPS_PORT}",
            f"--publish=127.0.0.1::{TCP_PORT}",
            image,
            "haproxy",
            "-W",
            "-db",
            "-S",
            "/run/haproxy/master.sock,mode,600,level,admin",
            "-f",
            "/data/generated/conf.d/",
        ],
        timeout=300,
    ).stdout.strip()
    try:
        proxy = Proxy(
            container,
            published_port(container, HTTP_PORT),
            published_port(container, HTTPS_PORT),
            published_port(container, TCP_PORT),
            collector,
        )
        wait_until_ready(proxy)
        yield proxy
    finally:
        run(["docker", "rm", "--force", container], check=False)


def wait_until_ready(proxy: Proxy) -> None:
    deadline = time.monotonic() + 20
    while True:
        try:
            proxy.get("app.example.com")
            return
        except (OSError, http.client.HTTPException):
            if time.monotonic() > deadline:
                raise AssertionError(f"HAProxy did not start:\n{proxy.logs()}") from None
            time.sleep(0.1)


def reload_with_saved_state(proxy: Proxy) -> None:
    """Save the server state and reload through the master CLI, as the apply pipeline does."""
    run(
        [
            "docker",
            "exec",
            proxy.container,
            "sh",
            "-c",
            'echo "show servers state" | socat - /run/haproxy/admin.sock'
            " > /data/state/haproxy-server-state",
        ]
    )
    result = run(
        [
            "docker",
            "exec",
            proxy.container,
            "sh",
            "-c",
            "echo reload | socat -t 30 - /run/haproxy/master.sock",
        ]
    )
    assert result.stdout.startswith("Success=1"), result.stdout


def upstream_reached(proxy: Proxy, expected: str) -> str:
    """Return the upstream address that answers, once it is `expected` or after a deadline."""
    deadline = time.monotonic() + 5
    while True:
        reached = proxy.get("app.example.com").body.split("|")[0]
        if reached == expected or time.monotonic() > deadline:
            return reached
        time.sleep(0.1)


def wait_for_log(proxy: Proxy, pattern: str) -> re.Match[str]:
    """Wait for an access line that the collector received and that matches `pattern`."""
    deadline = time.monotonic() + 15
    while True:
        lines = [entry.line for entry in proxy.collector.entries()]
        for line in lines:
            match = re.search(pattern, line)
            if match is not None:
                return match
        if time.monotonic() > deadline:
            text = "\n".join(lines[-50:])
            raise AssertionError(f"no log line matches {pattern!r}:\n{text}\n{proxy.logs()}")
        time.sleep(0.1)


def credentials(user: str, password: str) -> tuple[tuple[str, str]]:
    """The header of a basic auth request."""
    encoded = base64.b64encode(f"{user}:{password}".encode()).decode()
    return (("Authorization", f"Basic {encoded}"),)
