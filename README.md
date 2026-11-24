# HomeProxy

A self-hosted reverse proxy manager for home labs. One container bundles HAProxy with a small dashboard that only your LAN can reach. You manage routes, domains, certificates, DNS and protection from the dashboard, and you never edit `haproxy.cfg` by hand.

HAProxy does all the proxying. The dashboard writes its config and talks to its sockets, so traffic keeps flowing on the last good config if the dashboard stops.

> **Status: pre-release.** Releases publish an amd64 image. HomeProxy runs one real home lab so far, so expect rough edges.

## Who it is for

You run services on a few machines on your LAN, you have one or more domains, and your router forwards ports 80 and 443 to one host. You want to add `app.example.com` in front of `192.168.1.20:8080` and have it on HTTPS a minute later. Some services stay LAN-only, some go public with one switch, and certificates renew without you.

HomeProxy is for one admin and one host. The dashboard never faces the internet.

## What it does

- HTTP routes by host and path, TCP port forwards, TLS passthrough and redirects.
- Certificates from Let's Encrypt over the DNS challenge, with any DNS provider that lego supports, issued and renewed on their own. A domain can also have no certificate and serve plain HTTP.
- Routes that only the LAN can reach, and one switch to make a route public.
- An internal DNS server, so LAN devices resolve your routes to the proxy's LAN address.
- Cloudflare: DNS records for public routes, DDNS, and the real client IP behind the Cloudflare proxy.
- Protection: CrowdSec (bans, captcha, AppSec), GeoIP country blocking and basic auth.
- `haproxy -c` checks every change before it goes live. A failed apply rolls back, and every apply is a snapshot that you can restore.
- Access logs, metrics, a Prometheus exporter and notifications to Discord and webhooks.
- Your config stays in plain YAML files that you can read and edit.

## Install

You need a Linux machine with Docker Engine, the Compose plugin and a fixed LAN address. See [Install HomeProxy](docs/install.md). To move from an HAProxy you already run, see [Migrating](docs/migrating.md).

## Documentation

| Topic | Page |
| --- | --- |
| Routes | [Route types](docs/route-types.md), [Basic auth](docs/basic-auth.md) |
| Certificates and DNS | [Certificates](docs/certificates.md), [Internal DNS](docs/internal-dns.md), [Cloudflare DNS](docs/cloudflare-dns.md), [Real client IP](docs/real-client-ip.md) |
| Protection | [CrowdSec](docs/crowdsec.md), [GeoIP](docs/geoip.md) |
| Operation | [Changes and apply](docs/changes-and-apply.md), [Overview and status](docs/overview-and-status.md), [Dashboard access](docs/dashboard-access.md), [Notifications](docs/notifications.md), [Metrics and logs](docs/metrics-and-logs.md), [Prometheus](docs/prometheus.md) |

## Feedback and contributions

Ask for a feature with the feature request form in [Issues](https://github.com/GeorgeGedox/homeproxy/issues). First read the pinned "Not planned" issue, which lists what HomeProxy leaves out on purpose. Ask questions in [Discussions](https://github.com/GeorgeGedox/homeproxy/discussions). Report a security problem privately, as [SECURITY.md](SECURITY.md) says.

To work on the code, read [AGENTS.md](AGENTS.md). It has the commands, the code and test rules, and the branch workflow. The tools are pinned in `mise.toml`: run `mise install`, then `mise run dev` for the dev stack and `mise run check` before you push.

## License

HomeProxy is under the [MIT License](LICENSE). The image bundles HAProxy, lego and the CrowdSec SPOA bouncer, each under its own license.
