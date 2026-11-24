# CrowdSec and access logs

The image includes CrowdSec's SPOA bouncer, which bans and challenges clients in HAProxy. The CrowdSec engine, which reads HAProxy's access lines and decides whom to ban, runs in its own container. Add it to the `compose.yaml` from [Install HomeProxy](install.md): the `crowdsec` service under `services`, the `configs` block as a new top-level key, and the two `crowdsec-` volumes under the existing `volumes`. To paste one file instead, for example into Komodo, use [`deploy/compose.crowdsec.yaml`](../deploy/compose.crowdsec.yaml), which holds the whole stack with AppSec on, and skip the merges on this page. By hand, the additions are:

```yaml
services:
  homeproxy:
    # unchanged

  crowdsec:
    image: crowdsecurity/crowdsec:v1.8.2
    environment:
      COLLECTIONS: crowdsecurity/haproxy
      # Registers HomeProxy's bouncer with this key on the first start.
      BOUNCER_KEY_homeproxy: ${CROWDSEC_BOUNCER_KEY:?set CROWDSEC_BOUNCER_KEY in .env}
    configs:
      - source: crowdsec_haproxy
        target: /etc/crowdsec/acquis.d/haproxy.yaml
    ports:
      - "127.0.0.1:18080:8080"      # LAPI, for crowdsec.lapi_url
      - "127.0.0.1:15140:514/udp"   # syslog acquisition, for crowdsec.syslog
    volumes:
      - crowdsec-config:/etc/crowdsec
      - crowdsec-data:/var/lib/crowdsec/data
    restart: unless-stopped

configs:
  crowdsec_haproxy:
    content: |
      source: syslog
      listen_addr: 0.0.0.0
      listen_port: 514
      # HAProxy sends lines of up to 4096 bytes. The default, 2048, leaves long lines unparsed.
      max_message_len: 4096
      labels:
        type: syslog

volumes:
  homeproxy-data:
  crowdsec-config:
  crowdsec-data:
```

- `crowdsecurity/haproxy` brings the HAProxy log parser and the generic HTTP scenarios. The syslog acquisition receives HAProxy's access lines.
- HomeProxy runs on the host network, so the engine publishes its LAPI and its syslog acquisition (UDP) on `127.0.0.1` of the host, and HomeProxy uses those addresses.
- `configs` with `content` needs Docker Compose 2.23.1 or later. With an older Compose, put the acquisition in a file and mount it at the same path.

## Set up CrowdSec

1. Put a bouncer key in the `.env` file next to `compose.yaml`, then start the engine:

   ```sh
   echo "CROWDSEC_BOUNCER_KEY=$(openssl rand -hex 32)" >> .env
   docker compose up -d crowdsec
   ```

   The engine registers the bouncer only when it does not exist yet. To change the key later, run `docker compose exec crowdsec cscli bouncers delete homeproxy`, change `CROWDSEC_BOUNCER_KEY` in `.env`, run `docker compose up -d crowdsec` and set the new key on the Settings page.
2. Create the machine that the dashboard logs in with to list, add and delete decisions:

   ```sh
   docker compose exec crowdsec cscli machines add homeproxy-ui --password "$(openssl rand -hex 24)" -f -
   ```

   `-f -` prints the login and the password instead of writing them to the engine's own credentials file.
3. In HomeProxy, open Settings. Under CrowdSec, set the Bouncer key to the value of `CROWDSEC_BOUNCER_KEY`, the Machine ID to `homeproxy-ui` and the Machine password to the printed password. Keys are saved at once.
4. Turn on the CrowdSec switch, set CrowdSec API URL to `http://127.0.0.1:18080` and CrowdSec syslog address to `127.0.0.1:15140`, and press Save settings. Then press Apply in the bar at the bottom.
5. Check it. The CrowdSec section on Settings shows "Bouncer up" after a few seconds, and the Security page lists the decisions, if there are any yet. After some traffic, `docker compose exec crowdsec cscli metrics show acquisition parsers` shows lines read from `syslog` and parsed by `crowdsecurity/haproxy-logs`.

## Access lines

HAProxy sends every access line to two places:

- The dashboard's log collector, on a Unix socket inside the container. It feeds the live log tail and the metrics.
- CrowdSec's syslog acquisition, while `crowdsec.enabled` is true.

Access lines do not appear in the container log. The container log shows HAProxy's startup messages and messages about servers that go up or down.

## What CrowdSec can detect

CrowdSec reads HTTP access lines only. Its HAProxy parser (`crowdsecurity/haproxy-logs`) has no pattern for TCP lines. HAProxy still sends them, and CrowdSec counts them as received, but it does not parse them.

| Route type | Detection from its own traffic | Bans from CrowdSec |
| --- | --- | --- |
| `http` | Yes | Yes |
| `tcp` | No | Yes, for bans that CrowdSec made from other traffic or got from a blocklist |
| `passthrough` | No | Yes, for bans that CrowdSec made from other traffic or got from a blocklist |

## Settings

```yaml
crowdsec:
  enabled: true
  lapi_url: http://127.0.0.1:18080
  syslog: 127.0.0.1:15140
```

`crowdsec.syslog` must be an IPv4 address and a port, such as `127.0.0.1:15140`. A host name is refused. HAProxy resolves its log targets only when it starts or reloads. If a name does not resolve at that time, HAProxy does not start, and every site goes down.

The keys can also go in `secrets.yaml` instead of the Settings page. The apply fails while CrowdSec is on and the bouncer key is missing:

```yaml
# secrets.yaml
crowdsec:
  bouncer_key: "..."
  machine_id: homeproxy-ui
  machine_password: "..."
  # Only with a captcha provider. The signing key is at least 32 characters.
  captcha_site_key: "..."
  captcha_secret_key: "..."
  captcha_signing_key: "..."
```

The Settings page never shows a key, only whether it is set. A key you change on the Settings page reaches the bouncer at once: HomeProxy rewrites the bouncer's config and restarts it, with no apply. A key you change in the file reaches the bouncer at the next apply, so set it on the Settings page when nothing else is pending.

```yaml
crowdsec:
  captcha_provider: turnstile   # none, turnstile, hcaptcha or recaptcha
  allowlist: [203.0.113.7, 2001:db8::/32]
```

## Bans and captchas

HAProxy asks the bouncer about every HTTPS request and every new connection to a `tcp` or `passthrough` route, and the bouncer answers from the decisions it keeps in sync with the LAPI every 10 seconds.

- A banned client gets CrowdSec's ban page with status 403. A client that does not ask for HTML gets `Forbidden`.
- A client with a captcha decision gets the captcha page, if `crowdsec.captcha_provider` is set. Without a captcha provider, a captcha decision acts as a ban.
- With a captcha provider, HAProxy waits up to 1 second for the first 50 KB of a checked request's body, because the captcha answer arrives in it. Requests that are not checked never wait, so a slow upload from the LAN or the allowlist is not cut off.
- The bouncer checks the real client. On a route Cloudflare proxies, that is the visitor, never Cloudflare's edge (see [Real client IP behind Cloudflare](real-client-ip.md)). Access lines on the HTTP port name the visitor too. On any other route, a request through Cloudflare is checked and logged as Cloudflare's address.
- Requests from `network.lan_cidrs` and from `crowdsec.allowlist` are never checked, so you cannot ban yourself or a host you trust. A request through Cloudflare that names a LAN address is checked, because it comes from outside.
- A `tcp` or `passthrough` route cannot show a page, so HAProxy closes a connection from a client with a ban or captcha decision. The same clients as above are never checked. A `passthrough` route that Cloudflare proxies (see [Proxied routes](real-client-ip.md#proxied-routes-cannot-be-bypassed)) only sees Cloudflare's edge, so its connections are not checked. A `tcp` route has no Cloudflare option, so a connection from a Cloudflare address is checked like any other.
- If the bouncer is stopped or does not answer, requests and connections go through. Your sites never go down with it.

The bouncer starts after the first apply with `crowdsec.enabled: true`, and stops after an apply that turns it off. Each apply writes the bouncer's own config to `/data/crowdsec/spoa.yaml`, readable only by HomeProxy, and restarts the bouncer when it changed. If the bouncer stops on its own, for example while the LAPI is unreachable, it starts again after a few seconds.

## Decisions from the dashboard

The dashboard lists, adds and deletes CrowdSec decisions through the LAPI at `crowdsec.lapi_url`. It logs in as its own CrowdSec machine, from step 2 of the setup, not with the bouncer key. While CrowdSec is off, or the machine ID or password is missing, the Security page says so and its **Ban an address** button is disabled.

- The list shows every active ban and captcha, with its address or range, its origin and its time left. Decisions from blocklists can number thousands, so they are hidden behind **Show N from blocklists**; a search finds them too. The origin is `crowdsec` for a local detection, `cscli` for a manual decision (yours or one from the dashboard) and `CAPI` or `lists` for a blocklist.
- A manual ban or captcha is for an IP address or a range, for at least one second.
- Deleting a decision takes effect within about 10 seconds, when the bouncer pulls its next update. An address from a blocklist can come back at the blocklist's next pull, so **Unban** on such an address asks first and offers **Allowlist and unban**, which lets it in for good.

`cscli decisions` in the CrowdSec container still works, as a fallback.

## Allowlist

`crowdsec.allowlist` holds addresses and ranges that CrowdSec never checks. HAProxy keeps the list itself, in `allow.lst`, so a decision cannot block them.

An allowlist change from the dashboard takes effect at once, with no apply. HomeProxy saves it to `config.yaml`, changes the list in the running HAProxy and writes it to the live release, so it stays after a reload or a restart. An address is listed once, so `203.0.113.7` and `203.0.113.7/32` are the same entry. The change also adds a History entry, unless `/data/config` has a hand edit that is not applied yet (see [History and rollback](changes-and-apply.md#history-and-rollback)). Restoring an older snapshot brings back the allowlist it holds.

A range that reaches outside the private ranges, such as `198.51.0.0/16`, is added with a warning that names it, and every apply repeats the warning, because CrowdSec never checks any client in it. A single public address, such as one you allowlist from a ban, is no warning.

The dashboard refuses an allowlist change while a change to `config.yaml` is pending, for example a settings, domain or DNS change. Apply or discard that change first. Its copy of `config.yaml` holds the old allowlist. If you apply that copy, it undoes the allowlist change. Other pending changes, such as a route, do not stop an allowlist change.

If HAProxy does not answer, the change is still saved and the next apply uses it. Make the same change again to update HAProxy at once.

## AppSec (WAF)

CrowdSec's AppSec component blocks known attacks, such as a probe for `/.env`, before the request reaches your service. It is off by default. Turn it on with:

```yaml
crowdsec:
  appsec: true
  appsec_url: http://127.0.0.1:7422   # optional
```

- AppSec checks HTTPS requests only, never `tcp` or `passthrough` routes. Requests from `network.lan_cidrs` and from `crowdsec.allowlist` are not checked, as for bans.
- A blocked request gets the ban page with status 403. Only that request is refused: HomeProxy and the bouncer never add a decision. An AppSec challenge is answered the same way, with the ban page for that request.
- The CrowdSec engine can still ban the client's address. Each blocked request is an alert in the engine, and the `crowdsecurity/appsec-vpatch` scenario from the `appsec-virtual-patching` collection bans an address that trips two different rules within about a minute. With the default profile, that ban lasts 4 hours. One rule tripped many times, such as repeated `/.env` probes, does not ban. If a real user is banned by mistake, delete the decision in the dashboard. The engine never bans private addresses (its default whitelist), so test a ban from a public address, such as a phone on mobile data. To never ban from AppSec, remove the scenario in the CrowdSec container with `cscli scenarios remove crowdsecurity/appsec-vpatch`.
- AppSec also reads request bodies up to 50 KB. HAProxy waits up to 1 second for them, and sends larger bodies on without them. Treat AppSec as a light filter for known attacks, not a full firewall.
- If AppSec is stopped or does not answer, requests go through after at most 0.2 seconds. Your sites never go down with it.

The bouncer looks for AppSec at `crowdsec.appsec_url`. Without it, it uses port 7422, the AppSec component's default, on the host from `crowdsec.lapi_url`. The URL has a scheme, a host and an optional port, and no path. Change the `crowdsec` service from the top of this page in three places, then run `docker compose up -d crowdsec`. The `COLLECTIONS` line replaces the old one, it is not a second line:

```yaml
services:
  crowdsec:
    environment:
      # Replaces the COLLECTIONS line. Keep all three.
      COLLECTIONS: crowdsecurity/haproxy crowdsecurity/appsec-virtual-patching crowdsecurity/appsec-generic-rules
    configs:
      # next to crowdsec_haproxy
      - source: crowdsec_appsec
        target: /etc/crowdsec/acquis.d/appsec.yaml
    ports:
      # next to the other two
      - "127.0.0.1:7422:7422"       # AppSec, for crowdsec.appsec_url

configs:
  crowdsec_appsec:
    content: |
      source: appsec
      appsec_configs:
        - crowdsecurity/appsec-default
      listen_addr: 0.0.0.0:7422
      labels:
        type: appsec
```

AppSec accepts the bouncer's own key, so it needs no other key.

If the engine exits with `no appsec-config found for crowdsecurity/appsec-default`, the AppSec collections are missing from `COLLECTIONS`. Set the line above, with all three collections, and start the engine again.

## Log line format

HTTP lines use HAProxy's default `httplog` fields, and TCP lines use its default `tcplog` fields, so the CrowdSec parser reads them unchanged. Three quoted fields follow at the end of each line: the route name, the client's country code and the TLS server name (SNI). The SNI comes from the client, so it is URL-encoded: a normal host name stays the same, and a space or a quote becomes `%20` or `%22`. An empty field is `""`.

## Bouncer state

The CrowdSec card on the Security page and the CrowdSec section on the Settings page show whether the bouncer is up or down, as HAProxy's health check of the bouncer's socket sees it. HAProxy checks the socket every 2 seconds. While the bouncer is down, requests are not checked and pass through. The state is empty while CrowdSec is off.
