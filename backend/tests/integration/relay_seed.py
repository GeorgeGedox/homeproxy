"""Seed data for a HAProxy container with relays: the clients it sees, its ports, its files."""

from pathlib import Path

from homeproxy.store import load_tree

REPO_ROOT = Path(__file__).resolve().parents[3]
SEED = REPO_ROOT / "examples" / "data"
HTTP_PORT = 8080
HTTPS_PORT = 8443
CLOUDFLARE = "127.0.0.5"
LAN = "127.0.0.6"
OTHER = "127.0.0.7"
RELAY_PORTS = {CLOUDFLARE: 9105, LAN: 9106, OTHER: 9107}
PLACEHOLDER = f"""\
global
    stats socket /run/haproxy/admin.sock mode 600 level admin expose-fd listeners

defaults
    mode http
    timeout connect 5s
    timeout client 30s
    timeout server 30s

frontend placeholder
    bind :{HTTP_PORT}
    http-request return status 503
"""


# The upstream answers with the X-Forwarded-For and the number of CF-Connecting-IP headers it got.
# Each relay reports the source port of its connection to the HTTPS frontend, so a test can tell
# that two requests shared one connection. `http-reuse never` keeps that connection private to
# the client's session; by default a later request may take another session's idle connection.
RELAYS = """\
listen test_upstream
    bind 127.0.0.1:9000
    http-request return status 200 content-type text/plain \
lf-string "%[req.fhdr(x-forwarded-for)]|%[req.fhdr_cnt(cf-connecting-ip)]"
""" + "".join(
    f"""
listen relay_{port}
    bind :{port}
    http-reuse never
    http-response set-header X-Relay-Port %[bc_src_port]
    server https 127.0.0.1:{HTTPS_PORT} ssl verify none source {source}
"""
    for source, port in RELAY_PORTS.items()
)


PASSWORD_HASH = load_tree(SEED / "config" / "config.yaml")["dashboard"]["password_hash"]
