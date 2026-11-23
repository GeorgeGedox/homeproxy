"""The CrowdSec lab: the built image beside a CrowdSec engine, its seed data and its clients."""

import json
import os
import shutil
import socket
import ssl
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import httpx2
import pytest

from homeproxy.store import load_tree
from tests.conftest import wait_until
from tests.integration.haproxy_live import docker

CROWDSEC_IMAGE = (
    "crowdsecurity/crowdsec:v1.7.8"
    "@sha256:2f527c9bb8b367120eb08b82890aa912ce96bfa1ada93dda0721700e4b4e0dde"
)


REPO_ROOT = Path(__file__).resolve().parents[3]
SEED = REPO_ROOT / "examples" / "data"
BOUNCER_KEY = load_tree(SEED / "secrets" / "secrets.yaml")["crowdsec"]["bouncer_key"]
PASSWORD_HASH = load_tree(SEED / "config" / "config.yaml")["dashboard"]["password_hash"]
SEED_PASSWORD = "homeproxy-dev"  # noqa: S105 - the seeded dev password, see examples/data
STARTUP_TIMEOUT = 180.0
DECISION_TIMEOUT = 60.0
"""The bouncer pulls new decisions every 10 s."""


SOCKET = "/run/crowdsec-spoa/spoa.sock"
BOUNCER_CONFIG = "/data/crowdsec/spoa.yaml"
CLOUDFLARE = "127.0.0.5"
LAN = "127.0.0.6"
OUTSIDE = "127.0.0.7"
ALLOWED = "127.0.0.8"
RELAY_PORTS = {CLOUDFLARE: 9105, LAN: 9106, OUTSIDE: 9107, ALLOWED: 9108}
BANNED_VISITOR = "198.51.100.66"
CLEAN_VISITOR = "198.51.100.67"
BANNED = (CLOUDFLARE, LAN, OUTSIDE, ALLOWED, BANNED_VISITOR)


# The upstream answers with the client address that every rule saw.
RELAYS = """\
listen test_upstream
    bind 127.0.0.1:9000
    http-request return status 200 content-type text/plain lf-string "%[req.fhdr(x-forwarded-for)]"
""" + "".join(
    f"""
listen relay_{port}
    bind :{port}
    server https 127.0.0.1:8443 ssl verify none source {source}
"""
    for source, port in RELAY_PORTS.items()
)


# A `tcp` route, and a passthrough route behind the SNI layer, which then owns port 8443. The HTTP
# relays above send no SNI name, so their requests reach fe_https through the SNI layer too. TCP
# relays reach both routes from each source.
GAME_PORT = 9100
STREAM_PORTS = {GAME_PORT: 9110, 8443: 9120}
RELAYS += """
listen test_tls_upstream
    bind 127.0.0.1:9444 ssl crt /data/certs/default.pem
    http-request return status 200
""" + "".join(
    f"""
listen relay_{base + index} from tcp
    bind :{base + index}
    server target 127.0.0.1:{target} source {source}
"""
    for target, base in STREAM_PORTS.items()
    for index, source in enumerate(RELAY_PORTS)
)
STREAM_ROUTES = {
    "game.yaml": "version: 1\nname: game\ntype: tcp\nlisten_port: 9100\nexposure: public\n"
    "upstreams:\n  - address: 127.0.0.1:9000\n",
    "pass.yaml": "version: 1\nname: pass\ntype: passthrough\nhosts: [pass.example.com]\n"
    "exposure: public\nupstreams:\n  - address: 127.0.0.1:9444\n",
}


ROUTE = """\
version: 1
name: app
type: http
hosts: [app.example.com]
exposure: public
upstreams:
  - address: 127.0.0.1:9000
"""
# Cloudflare's edge names the visitor only on a route the zone proxies.
EDGE_ROUTE = ROUTE.replace("app", "edge") + "cloudflare: {dns: true, proxied: true}\n"


# Applies /data/config, then trusts only the test edge as Cloudflare until the next reload.
APPLY = f"""
import json
from pathlib import Path
from homeproxy.haproxy.client import HAProxyClient
from homeproxy.haproxy.pipeline import Pipeline
from homeproxy.store import Store
result = Pipeline(Store(Path("/data"))).apply("test")
if result.success:
    HAProxyClient().replace_acl("cloudflare.lst", ["{CLOUDFLARE}/32"])
print(json.dumps({{"success": result.success, "errors": result.errors}}))
"""


# Reads `bouncer` from `GET /status`, logged in as a LAN client on the dashboard's port.
STATUS = f"""
import http.client, json, ssl
context = ssl.create_default_context()
context.check_hostname = False
context.verify_mode = ssl.CERT_NONE
def call(method, path, body=None, cookie=""):
    connection = http.client.HTTPSConnection(
        "127.0.0.1", 9443, context=context, timeout=10, source_address=("{LAN}", 0)
    )
    headers = {{"Origin": "https://127.0.0.1:9443", "Content-Type": "application/json"}}
    if cookie:
        headers["Cookie"] = cookie
    connection.request(method, path, body=body, headers=headers)
    response = connection.getresponse()
    return response, response.read()
login, _ = call("POST", "/api/v1/auth/login", json.dumps({{"password": "{SEED_PASSWORD}"}}))
cookie = login.getheader("set-cookie").split(";", 1)[0]
status, body = call("GET", "/api/v1/status", cookie=cookie)
print(json.loads(body)["bouncer"])
"""


def config(enabled: bool, lapi: str) -> str:
    return f"""\
version: 1
dashboard:
  listen: 0.0.0.0:9443
  password_hash: "{PASSWORD_HASH}"
network:
  lan_cidrs: [{LAN}/32]
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
crowdsec:
  enabled: {"true" if enabled else "false"}
  lapi_url: http://{lapi}:8080
  syslog: {lapi}:514
  allowlist: [{ALLOWED}/32]
  # The fake keys reach no provider. They make HAProxy wait for small bodies, as captchas need.
  captcha_provider: turnstile
"""


def logs(container: str) -> str:
    result = subprocess.run(["docker", "logs", container], capture_output=True, text=True)
    return result.stdout + result.stderr


@dataclass(frozen=True)
class Lab:
    """The HomeProxy container and its address, and the CrowdSec engine's container and address."""

    homeproxy: str
    address: str
    crowdsec: str
    lapi: str

    def get(self, source: str, cf_value: str | None = None) -> httpx2.Response:
        host = "edge.example.com" if cf_value is not None else "app.example.com"
        headers = {"Host": host, "Accept": "text/html"}
        if cf_value is not None:
            headers["CF-Connecting-IP"] = cf_value
        url = f"http://{self.address}:{RELAY_PORTS[source]}/"
        return httpx2.get(url, headers=headers, timeout=10)

    def stream_served(self, source: str, target: int, hello_delay: float = 0.0) -> bool:
        """Return whether a connection from `source` to a port reaches the route's server. On the
        SNI layer's port, it is TLS for the passthrough host, which says hello after `hello_delay`
        seconds."""
        port = STREAM_PORTS[target] + list(RELAY_PORTS).index(source)
        reply = b""
        try:
            with socket.create_connection((self.address, port), timeout=10) as raw:
                client = raw
                if target != GAME_PORT:
                    # The delay is what the test is about, not a wait for something to finish.
                    time.sleep(hello_delay)
                    context = ssl.create_default_context()
                    context.check_hostname = False
                    context.verify_mode = ssl.CERT_NONE
                    client = context.wrap_socket(raw, server_hostname="pass.example.com")
                client.sendall(b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
                while chunk := client.recv(4096):
                    reply += chunk
        except OSError:
            return False
        return reply.startswith(b"HTTP/1.1 200 ")

    def apply(self, enabled: bool) -> None:
        """Write the config with CrowdSec on or off, as a hand edit, and apply it."""
        target = "/data/config/config.yaml"
        subprocess.run(
            ["docker", "exec", "-i", "-u", "homeproxy", self.homeproxy, "tee", target],
            input=config(enabled, self.lapi),
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        python = "/opt/homeproxy/venv/bin/python"
        out = docker("exec", "-u", "homeproxy", self.homeproxy, python, "-c", APPLY)
        result = json.loads(out.splitlines()[-1])
        assert result["success"], result

    def exists(self, path: str) -> bool:
        test = ["docker", "exec", self.homeproxy, "test", "-e", path]
        return subprocess.run(test, capture_output=True, check=False).returncode == 0

    def wait_for_ban(self) -> None:
        def banned() -> bool:
            return self.get(OUTSIDE).status_code == 403

        wait_until(
            banned,
            "the ban of the outside client",
            timeout=DECISION_TIMEOUT,
            interval=0.5,
            not_yet=(Exception,),
        )

    def wait_for_bouncer_down(self) -> None:
        wait_until(
            lambda: not self.exists(SOCKET),
            "the bouncer to stop",
            timeout=30,
            interval=0.5,
            not_yet=(Exception,),
        )

    def bouncer(self) -> str:
        """The bouncer state in `GET /status`: `up`, `down` or `None`."""
        python = "/opt/homeproxy/venv/bin/python"
        out = docker("exec", self.homeproxy, python, "-c", STATUS)
        return out.strip().splitlines()[-1]


def seed(directory: Path, lapi: str) -> None:
    (directory / "config" / "routes").mkdir(parents=True)
    (directory / "config" / "includes").mkdir()
    (directory / "config" / "config.yaml").write_text(config(True, lapi))
    (directory / "config" / "routes" / "app.yaml").write_text(ROUTE)
    (directory / "config" / "routes" / "edge.yaml").write_text(EDGE_ROUTE)
    for name, text in STREAM_ROUTES.items():
        (directory / "config" / "routes" / name).write_text(text)
    (directory / "config" / "includes" / "relays.cfg").write_text(RELAYS)
    shutil.copytree(SEED / "secrets", directory / "secrets")
    secrets = directory / "secrets" / "secrets.yaml"
    text = secrets.read_text()
    for name in ("captcha_site_key", "captcha_secret_key", "captcha_signing_key"):
        text = text.replace(f'{name}: ""', f'{name}: "fake-{name}-{"x" * 32}"')
    secrets.write_text(text)


def image() -> str:
    project = os.environ.get("COMPOSE_PROJECT_NAME")
    if not project:
        pytest.exit("COMPOSE_PROJECT_NAME is not set. Run `mise run test-integration`.", 2)
    return f"{os.environ.get('IMAGE_NAME', 'homeproxy')}:{project}"


def address_of(container: str, network: str) -> str:
    template = f'{{{{(index .NetworkSettings.Networks "{network}").IPAddress}}}}'
    return docker("inspect", "--format", template, container)


def start(prefix: str, seed_dir: Path) -> Lab:
    network, volume = f"{prefix}-net", f"{prefix}-data"
    crowdsec, homeproxy = f"{prefix}-crowdsec", f"{prefix}-homeproxy"
    docker("network", "create", network)
    docker(
        "run",
        "--detach",
        f"--name={crowdsec}",
        f"--network={network}",
        f"--env=BOUNCER_KEY_homeproxy={BOUNCER_KEY}",
        "--env=DISABLE_ONLINE_API=true",
        "--env=CROWDSEC_BYPASS_DB_VOLUME_CHECK=true",
        CROWDSEC_IMAGE,
    )
    lapi = address_of(crowdsec, network)
    seed(seed_dir, lapi)
    # As the dev stack's seed service does: the API user owns the files.
    copy = "cp -r /seed/. /data/ && chown -R homeproxy: /data && chmod 0600 /data/secrets/*"
    seed_volumes = [f"--volume={volume}:/data", f"--volume={seed_dir}:/seed:ro"]
    docker("run", "--rm", *seed_volumes, "--entrypoint=sh", image(), "-c", copy)
    docker(
        "run",
        "--detach",
        f"--name={homeproxy}",
        f"--network={network}",
        f"--volume={volume}:/data",
        "--env=HOMEPROXY_DEV_SELF_SIGNED=1",
        "--env=HOMEPROXY_CF_RANGES_URL=http://127.0.0.1:9",
        "--env=HOMEPROXY_RELEASES_URL=http://127.0.0.1:9/",
        "--env=HOMEPROXY_GEOIP_URL=http://127.0.0.1:9",
        image(),
    )
    return Lab(homeproxy, address_of(homeproxy, network), crowdsec, lapi)
