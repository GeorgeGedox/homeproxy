# HomeProxy image

One container: HAProxy 3.4 (official `haproxy` image, Debian trixie), the Python backend, lego and the CrowdSec SPOA bouncer. s6-overlay supervises the processes, and each one restarts on its own.

## Build and test

```sh
mise run image                       # build for this machine's architecture
mise run test-integration            # build, smoke test, integration tests
docker build -f image/Dockerfile .   # build only
```

`mise run image` tags `homeproxy:<COMPOSE_PROJECT_NAME>`, which the integration tests use.

CI and the release build amd64 and arm64, each on a native runner, and run the smoke, integration and end-to-end tests on both. Renovate updates the pinned versions and checksums (`renovate.json`): keep the `# vX` comment on each checksum line.

`image/test/smoke.sh` boots the image on random host ports and checks the placeholder, `/api/v1/healthz`, the unprivileged HAProxy user, the bundled binaries, and that killing or stopping the API leaves HAProxy serving.

## What is inside

| Path | Content |
| --- | --- |
| `/usr/local/sbin/haproxy` | HAProxy, with `cap_net_bind_service` as a file capability |
| `/opt/homeproxy/venv` | Python venv with the backend installed |
| `/usr/local/bin/lego` | lego, copied from the pinned `goacme/lego` image |
| `/usr/local/bin/crowdsec-spoa-bouncer` | SPOA bouncer from the pinned release archive |
| `/var/lib/crowdsec-haproxy-spoa-bouncer/html/` | Bouncer ban and captcha pages (`ban.html`, `ban-with-contact.html`, `captcha.html`) |
| `/opt/homeproxy/ui/` | The built Nuxt dashboard (static files from the `ui` stage). The API serves it on the dashboard port, and every non-API path without a file gets `index.html` |
| `/opt/homeproxy/placeholder/` | First-boot release, copied to `/data/releases/placeholder` |
| `/etc/s6-overlay/s6-rc.d/` | The services from `image/s6-rc.d/` |

Every base image, archive and page is pinned by version and digest or sha256 in the `ARG` lines at the top of `Dockerfile`. A new Debian or HAProxy patch changes the pins in one commit.

## Services

| Service | Runs as | Notes |
| --- | --- | --- |
| `init-data` (oneshot) | root | Creates `/data` and `/run` directories, fixes ownership, seeds the placeholder release |
| `haproxy` | `homeproxy` | `haproxy -W -S /run/haproxy/master.sock,... -f /data/generated/conf.d/` |
| `api` | `homeproxy` | `homeproxy serve`: uvicorn with one worker over HTTPS (`/data/tls/dashboard.pem`) |
| `dns`, `spoa` | `homeproxy` | The internal DNS server and the CrowdSec bouncer |
| `api-log`, `dns-log`, `spoa-log` | `homeproxy` | `scripts/service-log`: s6-log reads the service's stdout and stderr, passes every line on to the container log and stores it in `/data/state/logs/<service>/`. If that directory is not writable, it only passes lines on |

Each directory in `image/s6-rc.d/` is a service, and the build adds it to the `user` bundle. A new service needs `type`, `run` and `dependencies.d/init-data`. A service with a logger names it in `producer-for`, and the logger names the service in `consumer-for`. The run script sends stderr to stdout, because the logger reads only stdout.

Only the `init-data` oneshot can stop the container (`S6_BEHAVIOUR_IF_STAGE2_FAILS=2`). A longrun that crashes is restarted and never touches the others.

## Runtime

- Start as root. `/init` drops each service to the `homeproxy` user (uid 10001).
- Mount a volume on `/data`. The first start creates the data layout (`image/scripts/init-data`) and a placeholder release. `/data/generated` is a relative symlink to `releases/placeholder`.
- HAProxy needs `NET_BIND_SERVICE` in the container capability set (Docker's default). With `cap_drop: [ALL]`, add it back or HAProxy fails to execute.

| Variable | Default | Effect |
| --- | --- | --- |
| `HOMEPROXY_PLACEHOLDER_HTTP_PORT` | `80` | Port of the first-boot placeholder only. Generated releases use `network.http_port` |
| `HOMEPROXY_BOOTSTRAP_LISTEN` | port 8443 on the default route's address | Address of the dashboard in setup mode (no `config.yaml`). After setup, `dashboard.listen` applies. See `docs/dashboard-access.md` |

## Dev stack

```sh
mise run dev                 # build, start on the data volume as it is, print the URLs and setup token
mise run dev -- seed         # REPLACE the data volume with the example config, then start
mise run dev -- down         # stop, keep the data
mise run dev -- reset        # stop and delete the data volumes
mise run dev -- logs         # follow the logs (also: ps)
```

`deploy/compose.dev.yaml` runs these services:

| Service | Notes |
| --- | --- |
| `homeproxy` | The `dev` target of `image/Dockerfile`. `backend/homeproxy` is mounted read-only and uvicorn reloads on every change. HAProxy and the API sit on a bridge network, not host networking |
| `ui` | Nuxt dev server with hot reload (`ui-dev` target, `frontend/` mounted). It proxies `/api` to `homeproxy` |
| `crowdsec` | CrowdSec engine with the HAProxy collection and a syslog acquisition. Not published. The bouncer key is `crowdsec.bouncer_key` from the dev `secrets.yaml` |
| `whoami-a`, `whoami-b` | Test backends used by `examples/data/config/routes/whoami.yaml` |

**Host ports.** Fixed, on `127.0.0.1`: 8080 HAProxy HTTP, 8443 HAProxy HTTPS, 8000 the dashboard API, 3000 the Nuxt dev server, and 5335 the internal DNS server (UDP and TCP). If one is taken, Docker refuses to start and names it.

**Fixed addresses.** The dev network is `10.203.21.0/24`. `homeproxy` is always `10.203.21.4`, `whoami-a` `10.203.21.3` and `whoami-b` `10.203.21.2`, so the addresses that setup saves (`network.haproxy_lan_ip`, `dns_server.listen`) survive a recreate. Other containers get addresses from `10.203.21.128/25`. The subnet is outside Docker's default address pools (`172.17.0.0` to `172.31.255.255`, and `192.168.0.0/16`), so with those pools another compose project does not take it while the dev stack is down. It is also outside `10.0.x` and `10.1.x`, which home routers often use. If it clashes with a network on your machine, `up` fails with a pool overlap; change the subnet and the addresses in `deploy/compose.dev.yaml`.

**Empty by default.** `mise run dev` never seeds. A new data volume has no `config.yaml`, so HomeProxy starts in setup mode like a fresh install, and the data survives `down` and `up`. `up` prints the setup token. The `crowdsec` service runs with a fixed dev bouncer key either way; nothing uses it until CrowdSec is enabled.

**Seed data.** `mise run dev -- seed` stops the stack, **empties the data volume**, then `image/dev/make-seed` copies `examples/data/` with four changes and a one-shot `seed` container loads the result (`secrets.yaml` gets mode 0600). Then it starts the stack:

- listeners bind `0.0.0.0`, and `haproxy_lan_ip` is the container's pinned address, because `192.168.1.10` does not exist in a bridge network;
- the dev network's subnet joins `lan_cidrs`, so LAN-only routes answer the host browser;
- `cloudflare.enabled` and every `ddns_update` are `false`;
- CrowdSec points at the `crowdsec` service (`crowdsec.enabled` stays `false`).

Edit the files in the volume, run `seed` again, or `mise run dev -- reset` (deletes the volumes, back to setup mode) to start over. `examples/data/` itself is not changed.

**No real Cloudflare or ACME calls.** The seed turns Cloudflare off. `HOMEPROXY_DEV_SELF_SIGNED` defaults to `1` (self-signed certificates, no ACME at all). Set it to `0` to use Let's Encrypt staging with a test zone. As a last guard, `api.cloudflare.com` and the Let's Encrypt production host resolve to loopback inside `homeproxy`. CrowdSec runs without the online API.

## Limits

- In the dev stack, the API port serves the dashboard built into the image when it was built, so it does not follow source changes. Use the Nuxt dev server for hot reload.
- The dev `api` run script (`image/dev/api-run`) still serves `homeproxy.main:app` over plain HTTP with `uvicorn --reload`, without the source filter. The session gate and Origin check still apply.
- The dev `ui` service installs packages from the npm registry on its first start.
