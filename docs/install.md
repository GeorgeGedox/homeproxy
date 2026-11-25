# Install HomeProxy

HomeProxy is one container: HAProxy, the dashboard, the internal DNS server and the certificate tools. It runs on the host network of the machine that will answer on ports 80 and 443. To move from an HAProxy you already run, read [Migrating from a hand-written HAProxy config](migrating.md) after step 3 instead of freeing ports 80 and 443.

## Before you start

- A Linux machine with Docker Engine and the Compose plugin (`docker compose version`). The stack uses host networking, which is meant for Docker Engine on Linux.
- A fixed LAN address for that machine, for example `192.168.1.10`: a DHCP reservation on your router, or a static IP. Setup saves this address for HAProxy, the internal DNS server and the dashboard, and your LAN resolver and port forwards point at it. If it changes, LAN clients still ask the old address for DNS, and no code on this machine can fix that. HomeProxy keeps proxying and keeps the dashboard reachable (see [Dashboard access](dashboard-access.md#when-the-saved-address-is-gone)), but you must update the saved addresses on the Settings page and in your LAN resolver.
- Ports 80 and 443 free on that address, or spare ports for a trial run (see [Migrating](migrating.md)).
- A domain, and API credentials for its DNS provider, so HomeProxy can get certificates for it. The domain form lists every DNS provider that HomeProxy supports. On Cloudflare, that is an API token with the permissions Zone Read and DNS Edit (see [Certificates](certificates.md)). You need them in step 7, not before. A domain with no DNS provider works too, but it serves plain HTTP only.

## 1. Make a directory for the stack

```sh
mkdir ~/homeproxy
cd ~/homeproxy
curl -fsSLo compose.yaml https://raw.githubusercontent.com/GeorgeGedox/homeproxy/main/deploy/compose.yaml
```

## 2. Get the image

`compose.yaml` runs `ghcr.io/georgegedox/homeproxy:latest`, which each release publishes for amd64 and arm64. Compose pulls it in step 4. [Releases and updates](releases.md#image-tags) lists the other tags.

To build the image from source instead:

```sh
git clone https://github.com/GeorgeGedox/homeproxy.git homeproxy-src
docker build -t homeproxy:local -f homeproxy-src/image/Dockerfile homeproxy-src
```

Then set `HOMEPROXY_IMAGE=homeproxy:local` in step 3.

## 3. Choose your settings

The defaults need no changes when ports 80 and 443 are free. To change one, put it in a file named `.env` next to `compose.yaml`:

| Variable | Default | Meaning |
| --- | --- | --- |
| `HOMEPROXY_IMAGE` | `ghcr.io/georgegedox/homeproxy:latest` | The image to run. Name a version, such as `ghcr.io/georgegedox/homeproxy:0.1.0`, to update only when you change it. See [Releases and updates](releases.md#image-tags) |
| `HOMEPROXY_DATA` | volume `homeproxy-data` | What is mounted at `/data`. A volume name, or an absolute path such as `/srv/homeproxy` for a directory on local disk |
| `HOMEPROXY_BOOTSTRAP_LISTEN` | port 8443 on the default route's address | `ip:port` of the setup page. See [Dashboard access](dashboard-access.md) |
| `HOMEPROXY_PLACEHOLDER_HTTP_PORT` | `80` | Port of the page HAProxy serves before setup. Set it when something else holds port 80 |

These variables matter only until setup is done, except `HOMEPROXY_BOOTSTRAP_LISTEN`, which the dashboard also uses when it cannot listen on its saved address. Afterwards the ports come from `config.yaml`, which you change on the Settings page.

Keep `/data` on local disk, not on a network share: HAProxy reads it on every reload.

## 4. Start it

```sh
docker compose up -d
docker compose logs homeproxy
```

The log is long. Find the box that starts with "HomeProxy is not set up yet." It holds the setup address, a one-time setup token and the certificate's SHA-256 fingerprint:

```sh
docker compose logs homeproxy | grep -A12 "not set up yet"
```

Until you finish setup, HAProxy serves a placeholder page that says "HomeProxy is running" on port 80, or on `HOMEPROXY_PLACEHOLDER_HTTP_PORT`.

## 5. Finish setup

Open the address from the log, for example `https://192.168.1.10:8443/setup`, from a machine on your LAN. The root address redirects to the setup page until setup is done. Compare the fingerprint with the one in your browser before you accept the certificate warning. Enter the token, and choose a password of at least 10 characters and type it again. The wizard asks for nothing else.

HomeProxy fills in the network values itself, from the interface that holds the default route:

| Value | What setup writes | Example |
| --- | --- | --- |
| LAN ranges | The network of that interface, with its real netmask. If your browser's address is outside it, for example on another VLAN, that address is added too, so you can reach the dashboard after setup | `192.168.1.0/24` |
| Proxy LAN IP | The address of that interface | `192.168.1.10` |
| Dashboard address | The address the setup page is on now, which is `HOMEPROXY_BOOTSTRAP_LISTEN` when you set it | `192.168.1.10:8443` |
| DNS server address | The proxy LAN IP, port 5335 | `192.168.1.10:5335` |
| DNS upstreams | The public resolver `1.1.1.1`, for the names in your zones that HomeProxy does not answer itself | `1.1.1.1` |

Before you finish, the wizard shows the LAN ranges and the proxy LAN IP that it found. Change the LAN ranges, the proxy LAN IP and the DNS server address later on the Settings page (step 6), and the dashboard address as in [Change the dashboard address](dashboard-access.md#change-the-dashboard-address). If HomeProxy cannot detect a value, for example when the container has no default route, the wizard asks for it.

If the container runs on a Docker bridge network instead of the host network, the detected values are the container's own subnet and address. Change them on the Settings page to your LAN range and the host's LAN address.

The HTTP and HTTPS ports default to 80 and 443. The wizard asks for a port only when another program on the machine already listens on it, for example the proxy that HomeProxy replaces. See [Dashboard access](dashboard-access.md) for what the wizard does and how to reset the password.

When setup finishes, HomeProxy applies the new config, the wizard signs you in and opens the Overview page. HAProxy stops serving the placeholder page, nothing is pending, and the history has its first snapshot. You add domains on the Domains page (step 7). The dashboard stays on the address you are on.

If that first apply fails, setup still finishes. Overview shows the failed apply and its error, for example when a port you entered clashes with the dashboard or DNS port. HAProxy keeps the placeholder page. Fix the cause in step 6, then apply.

## 6. Settings that the wizard skips

Open Settings (menu on the left). Change what does not fit your machine, then press Save settings. It shows "Saved as a pending change", and the bar at the top counts it.

- **HTTP port and HTTPS port**, if ports 80 and 443 are not free. See [Migrating](migrating.md).
- **DNS upstreams**: the resolvers the internal DNS server asks for names in your zones that it does not answer itself. Setup sets `1.1.1.1`. To use others, type an address and press Enter. Never add your LAN resolver here. See [Internal DNS server](internal-dns.md).

The change takes effect at the next apply (step 8). A port that clashes with the dashboard, DNS or Prometheus port, or with a `tcp` route, is refused when you save, with a message such as `dns_server.listen port is already used by https_port`. The apply checks it again, for example after a hand edit.

## 7. Add your domain

Open Domains in the menu and choose Add domain. The page asks for:

| Field | Meaning |
| --- | --- |
| Domain | The zone, for example `example.com` |
| HTTPS | No certificate (the default, plain HTTP), Self-signed, Let's Encrypt via Cloudflare, or Let's Encrypt via another DNS provider, which you then pick from the list |
| Credentials | For Let's Encrypt: stored credentials of the same provider, or new ones. Cloudflare asks for an API token with Zone Read and DNS Edit on the zone. Another provider shows the variables that lego takes for it |
| Wildcard certificate | On by default: one certificate for the zone and `*.` the zone |
| DDNS record and the two switches below it | Only for Cloudflare, and optional. They belong to the Cloudflare DNS features |

For Cloudflare, HomeProxy checks that the token can read the zone and its DNS records. If it cannot, the page stays open and shows `The Cloudflare API token cannot read the zone example.com ... It needs Zone:Zone:Read and Zone:DNS:Edit on this zone`. HomeProxy cannot check the credentials of another provider when you save. The first issuance checks them. See [Certificates](certificates.md). The certificate is issued after you apply, in step 8.

## 8. Add a route and apply

1. Open Routes and add a route: a name such as `app`, a host such as `app.example.com`, the type `http`, exposure `lan` and an upstream such as `192.168.1.20:8096`.
2. Press Apply in the bar at the bottom. **Details** shows what changes first. A notice says "Applied" and the history gets a new snapshot. If the domain has a DNS provider, its first certificate is issued as a background job. Follow it on the Domains page, where the certificate shows "Issuing…" and then its days left. HTTPS uses a self-signed default certificate until the job finishes.
3. Overview shows the time of the last apply and the route as `UP` when HAProxy can reach the upstream.

How apply works, and how to roll back, is in [Pending changes, apply and history](changes-and-apply.md).

Test the route before you point anything at it:

```sh
curl -k --resolve app.example.com:443:192.168.1.10 https://app.example.com/
```

## 9. Point your network at it

- **Internal DNS.** In your LAN resolver, for example AdGuard Home or Pi-hole, send queries for your zone to the HomeProxy DNS server, `192.168.1.10:5335`. It forwards the other names in the zone to the DNS upstreams on the Settings page. See [Internal DNS server](internal-dns.md).
- **Router.** For routes with exposure `public`, forward ports 80 and 443 from the router to `192.168.1.10`. A LAN-only route needs no forward. How HomeProxy finds the real client address of a request that came through Cloudflare is in [Real client IP](real-client-ip.md).

## 10. Add CrowdSec (optional)

CrowdSec bans clients that attack your public routes. The bouncer is in the image, but the CrowdSec engine runs as a second service in `compose.yaml`. [CrowdSec and access logs](crowdsec.md) gives the service to copy and the setup steps.

## Back up

Copy `/data` to another machine, leaving out `generated`, `releases` and `state`. The next apply rebuilds them. Run this in the directory of `compose.yaml`:

```sh
docker compose exec -T homeproxy tar -C /data --exclude=./generated --exclude=./releases \
  --exclude=./state -czf - . > homeproxy-data.tar.gz
```

The archive holds `secrets/secrets.yaml` and the certificate keys. Keep it private.

To restore on a new machine, do steps 1 to 3, fill the empty data directory before the first start, then start:

```sh
docker compose run --rm --no-deps -T --entrypoint tar homeproxy -C /data -xzf - < homeproxy-data.tar.gz
docker compose up -d
```

HAProxy serves the placeholder page until an apply loads the restored config. Log in to the dashboard and press Apply in the bar at the bottom, or **Apply saved config** on History. A notice says "Applied" when the restored config is live. The password and certificates come back with the archive.

The `config/` directory is plain YAML. You can keep it in a private repository by hand. Never commit `secrets/`.

## Update

```sh
docker compose pull
docker compose up -d
```

With a version in `HOMEPROXY_IMAGE`, change it to the new version first. The [releases page](https://github.com/GeorgeGedox/homeproxy/releases) lists each version and its changes. With an image you built, rebuild it with the command from step 2 instead of `docker compose pull`.

Compose replaces the container and keeps `/data`. HAProxy is down for a few seconds. Config files that an older version wrote are migrated on start, and the originals are kept in `/data/migration-backups`. To go back to an earlier version, see [Releases and updates](releases.md#go-back-to-an-earlier-version).

## Stop and remove

```sh
docker compose down        # keeps /data
docker compose down -v     # also deletes the named volume: config, secrets and certificates
```

## If something is wrong

| Symptom | Cause and fix |
| --- | --- |
| No setup token in the log | The dashboard did not start. Read the log for the reason, for example the setup port is in use. Set `HOMEPROXY_BOOTSTRAP_LISTEN` |
| The setup page does not open | The address in the log is the default route's address. Use it, or set `HOMEPROXY_BOOTSTRAP_LISTEN` |
| The log shows HAProxy failing to bind a port | Another program holds the port. Free it, or set `HOMEPROXY_PLACEHOLDER_HTTP_PORT` for the placeholder and the ports on the Settings page for routes. The dashboard keeps working |
| A route stays `DOWN` | HAProxy cannot reach the upstream. Check its address from the HomeProxy machine |
| Certificate job failed | The error names the reason. See [Certificates](certificates.md) |
