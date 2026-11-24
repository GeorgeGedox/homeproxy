# HomeProxy

A self-hosted reverse proxy manager for home labs. One container bundles HAProxy with a small dashboard that only your LAN can reach. You manage routes, domains, certificates, DNS and protection from the dashboard. HAProxy does all the proxying: the dashboard writes its config and talks to its sockets, so traffic keeps flowing on the last good config if the dashboard stops.

> **Status: pre-release.** Release tags publish an amd64 image; arm64 is not built or tested yet. It runs one real home lab so far. Expect rough edges.

## What it does

- HTTP routes by host and path, TCP port forwards, TLS passthrough and redirects.
- Certificates from Let's Encrypt over the DNS challenge, issued and renewed without help. A domain can also have no certificate and serve plain HTTP.
- Routes that only the LAN can reach, and one switch to make a route public.
- An internal DNS server, so LAN devices resolve your routes to the proxy's LAN address.
- Cloudflare: DNS records for public routes, DDNS, and the real client IP behind the Cloudflare proxy.
- Protection: CrowdSec (bans, captcha, AppSec), GeoIP country blocking and basic auth.
- Every change is checked by `haproxy -c` before it goes live. Each apply is a snapshot that you can restore, and a failed apply rolls back.
- Access logs, metrics, a Prometheus exporter and notifications (Discord and webhooks).

## Install

You need a Linux machine with Docker Engine, the Compose plugin and a fixed LAN address. See [Install HomeProxy](docs/install.md). To move from an HAProxy you already run, see [Migrating](docs/migrating.md).

## Documentation

| Topic | Page |
| --- | --- |
| Routes | [Route types](docs/route-types.md), [Basic auth](docs/basic-auth.md) |
| Certificates and DNS | [Certificates](docs/certificates.md), [Internal DNS](docs/internal-dns.md), [Cloudflare DNS](docs/cloudflare-dns.md), [Real client IP](docs/real-client-ip.md) |
| Protection | [CrowdSec](docs/crowdsec.md), [GeoIP](docs/geoip.md) |
| Operation | [Changes and apply](docs/changes-and-apply.md), [Overview and status](docs/overview-and-status.md), [Dashboard access](docs/dashboard-access.md), [Notifications](docs/notifications.md), [Metrics and logs](docs/metrics-and-logs.md), [Prometheus](docs/prometheus.md) |
| Design | [Product design](PRODUCT_DESIGN.md), [Contracts](PLAN.md), [Design plan](docs/DESIGN_PLAN.md), [Decisions](docs/decisions.md) |

## Development

The tools are pinned in `mise.toml`. Run `mise install` once.

| Command | Purpose |
| --- | --- |
| `mise run dev` | Start the dev stack. `-- seed` loads the example data |
| `mise run check` | Lint, types, unit and golden tests |
| `mise run test-integration` | Container tests in Compose |
| `mise run test-e2e` | Build the image, then run the Playwright tests |
| `mise run image` | Build the amd64 image and smoke-test it |

The rules for code and tests are in [AGENTS.md](AGENTS.md).

## License

HomeProxy is under the [MIT License](LICENSE). The image bundles HAProxy, lego and the CrowdSec SPOA bouncer, each under its own license.
