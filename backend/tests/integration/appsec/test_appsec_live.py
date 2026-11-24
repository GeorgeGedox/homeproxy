"""CrowdSec AppSec through the bouncer in the built image, against a real AppSec component.

The image runs under s6 on a seeded volume, and the real pipeline applies the config inside it. A
CrowdSec engine in its own container runs the LAPI and the AppSec component with the virtual
patching rules. The test client is outside the LAN, and reaches the HTTPS port on the container's
own address, so no host port is bound.
"""

import json
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx2
import pytest

from tests.conftest import wait_until
from tests.integration.crowdsec_lab import (
    BOUNCER_KEY,
    CROWDSEC_IMAGE,
    PASSWORD_HASH,
    SEED,
    SOCKET,
    STARTUP_TIMEOUT,
    address_of,
    image,
    logs,
)
from tests.integration.haproxy_live import docker

APPSEC_ACQUISITION = """\
source: appsec
appsec_config: crowdsecurity/appsec-default
listen_addr: 0.0.0.0:7422
labels:
  type: appsec
"""
# The default AppSec config also loads the generic rules.
COLLECTIONS = "crowdsecurity/appsec-virtual-patching crowdsecurity/appsec-generic-rules"
PID_FILE = "/run/crowdsec-spoa/spoa.pid"
PROBE = "/.env"
UPSTREAM = """\
listen test_upstream
    bind 127.0.0.1:9000
    http-request return status 200 content-type text/plain string "upstream"
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
import json
from pathlib import Path
from homeproxy.haproxy.pipeline import Pipeline
from homeproxy.store import Store
result = Pipeline(Store(Path("/data"))).apply("test")
print(json.dumps({"success": result.success, "errors": result.errors}))
"""


def config(appsec: bool, lapi: str) -> str:
    return f"""\
version: 1
dashboard:
  listen: 0.0.0.0:9443
  password_hash: "{PASSWORD_HASH}"
network:
  lan_cidrs: [127.0.0.6/32]
  haproxy_lan_ip: 192.168.1.10
  http_port: 8080
  https_port: 8443
domains:
  - zone: example.com
    dns_provider: cloudflare
    credentials: cf-main
dns_server:
  listen: 127.0.0.1:5335
crowdsec:
  enabled: true
  lapi_url: http://{lapi}:8080
  syslog: {lapi}:514
  appsec: {"true" if appsec else "false"}
"""


@dataclass(frozen=True)
class Lab:
    homeproxy: str
    address: str
    crowdsec: str
    lapi: str

    def get(self, path: str) -> httpx2.Response:
        url = f"https://{self.address}:8443{path}"
        return httpx2.get(url, headers={"Host": "app.example.com"}, verify=False, timeout=10)

    def bouncer_pid(self) -> str:
        """The running bouncer's pid, or "" while there is none."""
        return docker("exec", self.homeproxy, "cat", PID_FILE, check=False)

    def has_socket(self, path: str) -> bool:
        test = ["docker", "exec", self.homeproxy, "test", "-S", path]
        return subprocess.run(test, capture_output=True, check=False).returncode == 0

    def apply(self, appsec: bool) -> None:
        """Write the config with AppSec on or off, apply it, and wait for the new bouncer."""
        old_pid = self.bouncer_pid()
        subprocess.run(
            [
                "docker",
                "exec",
                "-i",
                "-u",
                "homeproxy",
                self.homeproxy,
                "tee",
                "/data/config/config.yaml",
            ],
            input=config(appsec, self.lapi),
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        python = "/opt/homeproxy/venv/bin/python"
        out = docker("exec", "-u", "homeproxy", self.homeproxy, python, "-c", APPLY)
        result = json.loads(out.splitlines()[-1])
        assert result["success"], result

        # A request before then would pass whatever the config said, because the check fails open.
        def restarted() -> bool:
            return self.has_socket(SOCKET) and self.bouncer_pid() not in ("", old_pid)

        wait_until(
            restarted,
            "the bouncer to start on the new config",
            timeout=STARTUP_TIMEOUT,
            interval=0.5,
            not_yet=(Exception,),
        )

    def wait_for_appsec(self) -> None:
        wait_until(
            lambda: self.get(PROBE).status_code == 403,
            "AppSec",
            timeout=STARTUP_TIMEOUT,
            interval=0.5,
            not_yet=(Exception,),
        )


def start(prefix: str, seed_dir: Path) -> Lab:
    network, volume = f"{prefix}-net", f"{prefix}-data"
    crowdsec, homeproxy = f"{prefix}-crowdsec", f"{prefix}-homeproxy"
    acquisition = seed_dir / "appsec.yaml"
    acquisition.write_text(APPSEC_ACQUISITION)
    acquisition.chmod(0o644)
    docker("network", "create", network)
    docker(
        "run",
        "--detach",
        f"--name={crowdsec}",
        f"--network={network}",
        f"--env=BOUNCER_KEY_homeproxy={BOUNCER_KEY}",
        f"--env=COLLECTIONS={COLLECTIONS}",
        # The test client has a private address, which this default whitelist never lets ban.
        "--env=DISABLE_PARSERS=crowdsecurity/whitelists",
        "--env=DISABLE_ONLINE_API=true",
        "--env=CROWDSEC_BYPASS_DB_VOLUME_CHECK=true",
        f"--volume={acquisition}:/etc/crowdsec/acquis.d/appsec.yaml:ro",
        CROWDSEC_IMAGE,
    )
    lapi = address_of(crowdsec, network)
    data = seed_dir / "data"
    (data / "config" / "routes").mkdir(parents=True)
    (data / "config" / "includes").mkdir()
    (data / "config" / "config.yaml").write_text(config(True, lapi))
    (data / "config" / "routes" / "app.yaml").write_text(ROUTE)
    (data / "config" / "includes" / "upstream.cfg").write_text(UPSTREAM)
    shutil.copytree(SEED / "secrets", data / "secrets")
    # As the dev stack's seed service does: the API user owns the files.
    copy = "cp -r /seed/. /data/ && chown -R homeproxy: /data && chmod 0600 /data/secrets/*"
    seed_volumes = [f"--volume={volume}:/data", f"--volume={data}:/seed:ro"]
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
        "--env=HOMEPROXY_UPDATE_URL=http://127.0.0.1:9",
        "--env=HOMEPROXY_GEOIP_URL=http://127.0.0.1:9",
        image(),
    )
    return Lab(homeproxy, address_of(homeproxy, network), crowdsec, lapi)


@pytest.fixture(scope="session")
def lab(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Lab]:
    """CrowdSec with AppSec on, and AppSec answering. Once per worker, because the start is the
    slow part. Each test puts back what it changes."""
    prefix = f"homeproxy-test-appsec-{uuid.uuid4().hex[:12]}"
    try:
        running = start(prefix, tmp_path_factory.mktemp("seed"))
        lapi_status = ["docker", "exec", running.crowdsec, "cscli", "lapi", "status"]
        wait_until(
            lambda: subprocess.run(lapi_status, capture_output=True).returncode == 0,
            "the CrowdSec LAPI",
            timeout=STARTUP_TIMEOUT,
            interval=0.5,
        )
        admin = "/run/haproxy/admin.sock"
        wait_until(
            lambda: running.has_socket(admin),
            "HAProxy",
            timeout=STARTUP_TIMEOUT,
            interval=0.5,
            not_yet=(Exception,),
        )
        running.apply(appsec=True)
        running.wait_for_appsec()
        yield running
    except BaseException:
        for container in (f"{prefix}-homeproxy", f"{prefix}-crowdsec"):
            print(f"--- {container}\n{logs(container)[-6000:]}")
        raise
    finally:
        for kind, name in [
            ("container", f"{prefix}-homeproxy"),
            ("container", f"{prefix}-crowdsec"),
            ("volume", f"{prefix}-data"),
            ("network", f"{prefix}-net"),
        ]:
            subprocess.run(["docker", kind, "rm", "--force", name], capture_output=True)


def test_env_probe_gets_the_ban_page_with_appsec_on(lab: Lab) -> None:
    probe = lab.get(PROBE)

    assert probe.status_code == 403
    assert "<title>CrowdSec Ban</title>" in probe.text
    assert lab.get("/").text == "upstream"


def test_two_different_rules_ban_the_client(lab: Lab) -> None:
    """`docs/crowdsec.md` says the hub's `crowdsecurity/appsec-vpatch` scenario bans a client that
    trips two different rules within about a minute."""
    cscli = ["docker", "exec", lab.crowdsec, "cscli", "decisions"]
    scenario = ["--scenario", "crowdsecurity/appsec-vpatch"]

    def banned() -> bool:
        listed = subprocess.run([*cscli, "list", *scenario, "-o", "json"], capture_output=True)
        return bool(json.loads(listed.stdout or "null"))

    try:
        assert lab.get("/.git/config").status_code == 403
        assert lab.get(PROBE).status_code == 403
        wait_until(banned, "the AppSec ban", timeout=STARTUP_TIMEOUT, interval=0.5)
    finally:
        subprocess.run([*cscli, "delete", *scenario], capture_output=True)
        # The bouncer pulls the removal within 10 s.
        wait_until(
            lambda: lab.get("/").status_code == 200,
            "the unban",
            timeout=STARTUP_TIMEOUT,
            interval=0.5,
            not_yet=(Exception,),
        )


def test_env_probe_passes_with_appsec_off(lab: Lab) -> None:
    lab.apply(appsec=False)
    try:
        assert lab.get(PROBE).text == "upstream"
    finally:
        lab.apply(appsec=True)
    lab.wait_for_appsec()


def test_requests_are_served_while_appsec_does_not_answer(lab: Lab) -> None:
    """A frozen engine accepts connections and never answers, the slowest way for AppSec to fail."""
    docker("pause", lab.crowdsec)
    try:
        started = time.monotonic()
        assert lab.get(PROBE).text == "upstream"
        # Fail-open must not cost more than the SPOE processing timeout.
        assert time.monotonic() - started < 1.0
    finally:
        docker("unpause", lab.crowdsec)
    lab.wait_for_appsec()
