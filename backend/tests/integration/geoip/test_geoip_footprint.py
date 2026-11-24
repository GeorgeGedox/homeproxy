"""The footprint with the largest country map the builder writes, in the built image.

The image runs as in production, under s6, with last month's small map. The admin starts a
refresh through the API, which downloads a DB-IP file from a server inside the container. The file
alternates two countries per /24 and per IPv6 /32, which no reduction can shrink, so the map lands
just under `MAX_ENTRIES`. The refresh builds it in a child process and applies it, as the monthly
job does. With no country blocked, HAProxy holds none of it. Then the admin blocks one of the two
countries, which puts half the ranges, and so nearly the whole map, in HAProxy. The test measures
each apply, made through the API as the admin makes it, and the idle memory of the whole container
after it.
"""

import gzip
import ipaddress
import json
import re
import shutil
import subprocess
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from homeproxy.geoip.country_map import MAX_ENTRIES
from homeproxy.geoip.data import GeoipData, save_country_map
from tests.conftest import wait_until
from tests.integration.crowdsec_lab import PASSWORD_HASH, SEED, SEED_PASSWORD, image, logs
from tests.integration.dashboard_client import CLIENT_SCRIPT
from tests.integration.haproxy_live import docker

PYTHON = "/opt/homeproxy/venv/bin/python"
DASHBOARD = ("127.0.0.1", "9443")
"""The dashboard's address inside the container. Loopback is in `lan_cidrs`."""
REFRESH = "/api/v1/geoip/refresh"
APPLY_LIMIT = 5.0
MEMORY_LIMIT = 300_000_000
"""300 MB, the idle footprint budget for the whole container."""
NO_BLOCK_LIMIT = 274_800_000 - 80_000_000
"""80 MB below the lowest of five runs from when HAProxy held the whole map."""
REFRESH_TIMEOUT = 180.0
"""The download, build and apply of the largest map on a slow machine."""
DOWNLOADS_PORT = 18765
"""Inside the container's own network namespace, never on the host."""
HAPROXY = ("127.0.0.1", "8443")
IPV4_ROWS = 2 * (MAX_ENTRIES * 3 // 4 - 1000)
IPV6_ROWS = 2 * (MAX_ENTRIES // 4 - 1000)

CONFIG = f"""\
version: 1
dashboard:
  listen: 0.0.0.0:9443
  password_hash: "{PASSWORD_HASH}"
network:
  lan_cidrs: [192.168.1.0/24, 127.0.0.0/8]
  haproxy_lan_ip: 192.168.1.10
  http_port: 8080
  https_port: 8443
domains:
  - zone: example.com
    dns_provider: cloudflare
    credentials: cf-main
dns_server:
  listen: 127.0.0.1:5335
"""
UPSTREAM = """\
listen test_upstream
    bind 127.0.0.1:9000
    http-request return status 200
"""
ROUTE = """\
version: 1
name: app
type: http
hosts: [app.example.com]
exposure: public
upstreams:
  - address: 127.0.0.1:9000
"""

APPLY = """
import http.client, json, ssl, sys, time
host, port, headers = sys.argv[1], int(sys.argv[2]), json.loads(sys.argv[3])
context = ssl.create_default_context()
context.check_hostname = False
context.verify_mode = ssl.CERT_NONE
connection = http.client.HTTPSConnection(host, port, context=context, timeout=60)
started = time.monotonic()
connection.request("POST", "/api/v1/apply", headers=headers)
body = json.loads(connection.getresponse().read())
seconds = time.monotonic() - started
print(json.dumps({"success": body["success"], "errors": body["errors"], "seconds": seconds}))
"""
"""`POST /apply` from inside the container, timed around the request only."""


def row(network: ipaddress.IPv4Network | ipaddress.IPv6Network, n: int) -> str:
    return f"{network.network_address},{network.broadcast_address},{'DE' if n % 2 else 'FR'}\n"


def dbip_rows() -> str:
    """Rows that alternate two countries per IPv4 /24 and per IPv6 /32. Loopback is in DE, so
    the test's own requests have a country that is not blocked."""
    ipv4 = int(ipaddress.IPv4Address("1.0.0.0"))
    ipv6 = int(ipaddress.IPv6Address("2001::"))
    rows = [row(ipaddress.IPv4Network((ipv4 + (n << 8), 24)), n) for n in range(IPV4_ROWS)]
    rows.append(row(ipaddress.IPv4Network("127.0.0.0/8"), 1))
    rows += [row(ipaddress.IPv6Network((ipv6 + (n << 96), 32)), n) for n in range(IPV6_ROWS)]
    return "".join(rows)


def seed(directory: Path) -> None:
    (directory / "config" / "routes").mkdir(parents=True)
    (directory / "config" / "includes").mkdir()
    (directory / "config" / "config.yaml").write_text(CONFIG)
    (directory / "config" / "routes" / "app.yaml").write_text(ROUTE)
    (directory / "config" / "includes" / "upstream.cfg").write_text(UPSTREAM)
    shutil.copytree(SEED / "secrets", directory / "secrets")
    downloads = directory / "downloads" / "free"
    downloads.mkdir(parents=True)
    name = f"dbip-country-lite-{datetime.now(UTC):%Y-%m}.csv.gz"
    (downloads / name).write_bytes(gzip.compress(dbip_rows().encode(), compresslevel=1))
    # Fresh data, so the monthly job does not start a second refresh during the test.
    info = GeoipData(
        provider="dbip-lite", updated_at=datetime.now(UTC), entries=1, countries=["DE"]
    )
    save_country_map(directory, "1.0.0.0/24 DE\n", info)


def exec_output(container: str, *command: str) -> str:
    result = subprocess.run(
        ["docker", "exec", container, *command], capture_output=True, text=True, timeout=60
    )
    return result.stdout if result.returncode == 0 else ""


def dashboard(
    container: str, steps: list[dict[str, Any]], address: tuple[str, str] = DASHBOARD
) -> list[dict[str, Any]]:
    """Send HTTPS requests from inside the container, by default to the dashboard on loopback."""
    out = docker("exec", container, PYTHON, "-c", CLIENT_SCRIPT, *address, json.dumps(steps))
    results: list[dict[str, Any]] = json.loads(out)
    return results


def start_refresh(container: str) -> dict[str, str]:
    """Log in and run a GeoIP refresh, as the admin does from the dashboard. Return the headers
    of the session once the refresh job has finished."""
    origin = {"Origin": f"https://{DASHBOARD[0]}:{DASHBOARD[1]}"}
    healthz = [{"method": "GET", "path": "/api/v1/healthz"}]
    wait_until(
        lambda: "status" in dashboard(container, healthz)[0],
        "the dashboard",
        timeout=120,
        interval=1.0,
    )
    body = {"password": SEED_PASSWORD}
    login = {"method": "POST", "path": "/api/v1/auth/login", "headers": origin, "body": body}
    [answer] = dashboard(container, [login])
    assert answer["status"] == 204, answer
    cookie = next(value for key, value in answer["headers"] if key == "set-cookie")
    headers = origin | {"Cookie": cookie.split(";", 1)[0]}
    [answer] = dashboard(container, [{"method": "POST", "path": REFRESH, "headers": headers}])
    assert answer["status"] == 202, answer
    job_path = f"/api/v1/jobs/{json.loads(answer['body'])['id']}"
    job = {"method": "GET", "path": job_path, "headers": headers}

    def finished() -> bool:
        [answer] = dashboard(container, [job])
        state = json.loads(answer["body"])
        assert state["state"] != "failed", state
        return bool(state["state"] == "finished")

    wait_until(
        finished,
        "the refresh to build and apply the country map",
        timeout=REFRESH_TIMEOUT,
        interval=1.0,
    )
    return headers


def memory_bytes(container: str) -> float:
    """The container's memory as `docker stats` shows it, without the page cache."""
    usage = docker("stats", "--no-stream", "--format", "{{.MemUsage}}", container)
    match = re.match(r"([0-9.]+)([KMG]?i?B)", usage)
    assert match is not None, usage
    units = {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3}
    return float(match[1]) * units[match[2]]


@pytest.fixture(scope="module")
def container(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[str, dict[str, str]]]:
    """The image after its first GeoIP refresh, with the largest map the builder writes."""
    prefix = f"homeproxy-test-geoip-{uuid.uuid4().hex[:12]}"
    name, volume = f"{prefix}-homeproxy", f"{prefix}-data"
    seed_dir = tmp_path_factory.mktemp("seed")
    seed(seed_dir)
    try:
        copy = "cp -r /seed/. /data/ && chown -R homeproxy: /data && chmod 0600 /data/secrets/*"
        seed_volumes = [f"--volume={volume}:/data", f"--volume={seed_dir}:/seed:ro"]
        docker("run", "--rm", *seed_volumes, "--entrypoint=sh", image(), "-c", copy)
        docker(
            "run",
            "--detach",
            f"--name={name}",
            f"--volume={volume}:/data",
            "--env=HOMEPROXY_DEV_SELF_SIGNED=1",
            "--env=HOMEPROXY_CF_RANGES_URL=http://127.0.0.1:9",
            "--env=HOMEPROXY_RELEASES_URL=http://127.0.0.1:9/",
            "--env=HOMEPROXY_UPDATE_URL=http://127.0.0.1:9",
            f"--env=HOMEPROXY_GEOIP_URL=http://127.0.0.1:{DOWNLOADS_PORT}",
            image(),
        )
        serve = f"echo $$ > /tmp/downloads.pid && exec {PYTHON} -m http.server {DOWNLOADS_PORT}"
        serve += " --bind 127.0.0.1 -d /data/downloads"
        docker("exec", "--detach", "-u", "homeproxy", name, "sh", "-c", serve)
        headers = start_refresh(name)
        info = json.loads(exec_output(name, "cat", "/data/geoip/country.json"))
        assert info["entries"] > MAX_ENTRIES - 3000
        assert info["countries"] == ["DE", "FR"]
        # The download server is part of the test, not of the footprint.
        docker("exec", name, "sh", "-c", "kill $(cat /tmp/downloads.pid)")
        yield name, headers
    except BaseException:
        print(f"--- {name}\n{logs(name)[-6000:]}")
        raise
    finally:
        for kind, item in [("container", name), ("volume", volume)]:
            subprocess.run(["docker", kind, "rm", "--force", item], capture_output=True)


def edit_route(container: str) -> None:
    """Edit the route by hand, so the next apply has something to change."""
    edited = ROUTE + 'response_headers: {X-Test: "1"}\n'
    target = "/data/config/routes/app.yaml"
    tee = ["docker", "exec", "-i", "-u", "homeproxy", container, "tee", target]
    subprocess.run(tee, input=edited, capture_output=True, text=True, check=True, timeout=30)


def timed_apply(container: str, headers: dict[str, str]) -> float:
    """Apply through the API, as the admin does, so the API process renders the release. Return
    the seconds the request took, without the time `docker exec` takes on a busy machine."""
    out = docker("exec", container, PYTHON, "-c", APPLY, *DASHBOARD, json.dumps(headers))
    result = json.loads(out.splitlines()[-1])
    assert result["success"], result
    return float(result["seconds"])


def idle_memory(container: str) -> float:
    """The container's memory once the HAProxy worker of the last reload is the only one."""

    def one_worker() -> bool:
        processes = exec_output(container, "sh", "-c", "cat /proc/[0-9]*/comm")
        return processes.split().count("haproxy") == 2

    # After a reload, the old worker exits once its connections close.
    wait_until(one_worker, "the old HAProxy worker to exit", timeout=60, interval=1.0)
    return memory_bytes(container)


def countries_counted(container: str, headers: dict[str, str]) -> list[str | None]:
    """Send one request through HAProxy and return the countries in the last hour's metrics."""
    request = {"method": "GET", "path": "/", "headers": {"Host": "app.example.com"}}
    assert dashboard(container, [request], HAPROXY)[0]["status"] == 200
    series = {"method": "GET", "path": "/api/v1/metrics/series?range=1h&group=country"}
    [answer] = dashboard(container, [series | {"headers": headers}])
    return [point["group"] for point in json.loads(answer["body"])]


def test_largest_dataset_keeps_apply_under_5_s_and_idle_memory_low(
    container: tuple[str, dict[str, str]],
) -> None:
    """One test, because each run of the fixture waits for a refresh of the largest map."""
    name, headers = container

    edit_route(name)
    assert timed_apply(name, headers) < APPLY_LIMIT
    assert idle_memory(name) < NO_BLOCK_LIMIT

    # HAProxy logs no country here, so the collector finds it in the full data.
    wait_until(
        lambda: "DE" in countries_counted(name, headers),
        "DE in the metrics",
        timeout=60,
        interval=1.0,
    )

    block = {"method": "PUT", "path": "/api/v1/geoip", "body": {"block_countries": ["FR"]}}
    assert dashboard(name, [block | {"headers": headers}])[0]["status"] == 204
    assert timed_apply(name, headers) < APPLY_LIMIT
    live = exec_output(name, "wc", "-l", "/data/generated/country.map")
    assert int(live.split(" ")[0]) > MAX_ENTRIES - 3000
    assert idle_memory(name) < MEMORY_LIMIT
