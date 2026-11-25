"""The warning about routes that Cloudflare proxies by hand, in the built image.

The image runs as in production, under s6, with Cloudflare on. One public route is not marked
proxied and one is. The test copy of `cloudflare.lst` in the live release holds a loopback address,
as a refresh would write the real ranges, so a request from that address looks like one from the
Cloudflare edge to the log collector. Only the route that is not marked proxied shows in `/status`.
"""

import json
import shutil
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import NOWHERE, wait_until
from tests.integration.crowdsec_lab import PASSWORD_HASH, SEED, SEED_PASSWORD, image, logs
from tests.integration.dashboard_client import CLIENT_SCRIPT, PYTHON
from tests.integration.haproxy_live import docker

DASHBOARD = ("127.0.0.1", "9443")
ORIGIN = {"Origin": f"https://{DASHBOARD[0]}:{DASHBOARD[1]}"}
EDGE = "127.0.0.2"
"""A loopback address in the test `cloudflare.lst`. It is not in `lan_cidrs`."""
STARTUP_TIMEOUT = 120.0
WARNING_TIMEOUT = 60.0

CONFIG = f"""\
version: 1
dashboard:
  listen: 0.0.0.0:9443
  password_hash: "{PASSWORD_HASH}"
network:
  lan_cidrs: [192.168.1.0/24, 127.0.0.1/32]
  haproxy_lan_ip: 192.168.1.10
  http_port: 8080
  https_port: 8443
cloudflare:
  enabled: true
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
name: {name}
type: http
hosts: [{name}.example.com]
exposure: public
upstreams:
  - address: 127.0.0.1:9000
cloudflare: {cloudflare}
"""
ROUTES = {
    "app": ROUTE.format(name="app", cloudflare="{dns: true, proxied: false}"),
    "edge": ROUTE.format(name="edge", cloudflare="{dns: true, proxied: true}"),
}

REQUEST = """
import http.client, ssl, sys
context = ssl.create_default_context()
context.check_hostname = False
context.verify_mode = ssl.CERT_NONE
connection = http.client.HTTPSConnection(
    "127.0.0.1", 8443, context=context, timeout=8, source_address=(sys.argv[1], 0)
)
connection.request("GET", "/", headers={"Host": sys.argv[2]})
print(connection.getresponse().status)
"""
"""One request through HAProxy from a chosen source address to a host."""


def seed(directory: Path) -> None:
    (directory / "config" / "routes").mkdir(parents=True)
    (directory / "config" / "includes").mkdir()
    (directory / "config" / "config.yaml").write_text(CONFIG)
    for name, text in ROUTES.items():
        (directory / "config" / "routes" / f"{name}.yaml").write_text(text)
    (directory / "config" / "includes" / "upstream.cfg").write_text(UPSTREAM)
    shutil.copytree(SEED / "secrets", directory / "secrets")


def dashboard(container: str, steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Send HTTPS requests from inside the container to the dashboard on loopback."""
    out = docker("exec", container, PYTHON, "-c", CLIENT_SCRIPT, *DASHBOARD, json.dumps(steps))
    results: list[dict[str, Any]] = json.loads(out)
    return results


def log_in_and_apply(container: str) -> dict[str, str]:
    """Wait for the dashboard, log in and apply the seeded config. Return the session headers."""
    healthz = [{"method": "GET", "path": "/api/v1/healthz"}]
    wait_until(
        lambda: "status" in dashboard(container, healthz)[0],
        "the dashboard",
        timeout=STARTUP_TIMEOUT,
        interval=1.0,
    )
    body = {"password": SEED_PASSWORD}
    login = {"method": "POST", "path": "/api/v1/auth/login", "headers": ORIGIN, "body": body}
    [answer] = dashboard(container, [login])
    assert answer["status"] == 204, answer
    cookie = next(value for key, value in answer["headers"] if key == "set-cookie")
    headers = ORIGIN | {"Cookie": cookie.split(";", 1)[0]}
    [answer] = dashboard(
        container, [{"method": "POST", "path": "/api/v1/apply", "headers": headers}]
    )
    assert answer["status"] == 200 and json.loads(answer["body"])["success"], answer
    return headers


def write_edge_ranges(container: str) -> None:
    """Replace `cloudflare.lst` of the live release with the test edge address."""
    tee = [
        "docker",
        "exec",
        "-i",
        "-u",
        "homeproxy",
        container,
        "tee",
        "/data/generated/cloudflare.lst",
    ]
    subprocess.run(
        tee, input=f"{EDGE}/32\n", capture_output=True, text=True, check=True, timeout=30
    )


@pytest.fixture(scope="module")
def container(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[str, dict[str, str]]]:
    """The image with an applied config, whose live `cloudflare.lst` holds `EDGE`."""
    prefix = f"homeproxy-test-cfedge-{uuid.uuid4().hex[:12]}"
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
            f"--env=HOMEPROXY_CF_RANGES_URL={NOWHERE}",
            f"--env=HOMEPROXY_RELEASES_URL={NOWHERE}/",
            f"--env=HOMEPROXY_UPDATE_URL={NOWHERE}",
            f"--env=CLOUDFLARE_BASE_URL={NOWHERE}",
            image(),
        )
        headers = log_in_and_apply(name)
        write_edge_ranges(name)
        yield name, headers
    except BaseException:
        print(f"--- {name}\n{logs(name)[-6000:]}")
        raise
    finally:
        for kind, item in [("container", name), ("volume", volume)]:
            subprocess.run(["docker", kind, "rm", "--force", item], capture_output=True)


def unmarked_after_requests(container: str, headers: dict[str, str]) -> list[str]:
    """Send one request from `EDGE` to each route. Return `cloudflare_unmarked` of `/status`."""
    for host in ("app.example.com", "edge.example.com"):
        docker("exec", container, PYTHON, "-c", REQUEST, EDGE, host)
    status = {"method": "GET", "path": "/api/v1/status", "headers": headers}
    [answer] = dashboard(container, [status])
    assert answer["status"] == 200, answer
    unmarked: list[str] = json.loads(answer["body"])["cloudflare_unmarked"]
    return unmarked


def test_only_the_route_that_is_not_marked_proxied_warns_about_cloudflare_requests(
    container: tuple[str, dict[str, str]],
) -> None:
    name, headers = container

    wait_until(
        lambda: unmarked_after_requests(name, headers) == ["app"],
        "the warning for the route that is not marked proxied",
        timeout=WARNING_TIMEOUT,
        interval=2.0,
    )
