# CrowdSec and access logs

The image includes the CrowdSec SPOA bouncer. It bans clients and shows them a challenge in HAProxy. The CrowdSec engine runs in its own container. It reads the access lines of HAProxy and decides whom to ban.

## Set up CrowdSec

1. Add the engine to your stack. See [Engine container](#engine-container). Put a bouncer key in the `.env` file next to `compose.yaml`, then start the engine:

   ```sh
   echo "CROWDSEC_BOUNCER_KEY=$(openssl rand -hex 32)" >> .env
   docker compose up -d crowdsec
   ```

   The engine registers the bouncer only when it does not exist yet. To change the key later, do these steps:
   1. Run `docker compose exec crowdsec cscli bouncers delete homeproxy`.
   2. Change `CROWDSEC_BOUNCER_KEY` in `.env`.
   3. Run `docker compose up -d crowdsec`.
   4. Set the new key in the dashboard, as in step 3.
2. Make the machine that the dashboard uses to log in. The dashboard uses it to list, add and delete decisions:

   ```sh
   docker compose exec crowdsec cscli machines add homeproxy-ui --password "$(openssl rand -hex 24)" -f -
   ```

   `-f -` prints the login and the password. Without it, the command writes them to the credentials file of the engine.
3. In the dashboard, open **Settings** and select **CrowdSec**. For each of **Bouncer key**, **Machine ID** and **Machine password**, click **Add**, enter the value and click **Save key**. Use the value of `CROWDSEC_BOUNCER_KEY`, `homeproxy-ui` and the printed password. A key is saved at once.
4. Turn on the **CrowdSec** switch. Set **CrowdSec API URL** to `http://127.0.0.1:18080`. Set **CrowdSec syslog address** to `127.0.0.1:15140`. Click **Stage changes**, then click **Apply** in the bar at the bottom.
5. Check the setup:
   - The CrowdSec section shows "Bouncer up" after a few seconds.
   - Click **Test connection** in that section. See [Connection test](#connection-test).
   - The **Security** page lists the decisions, if there are any.
   - After some traffic, run `docker compose exec crowdsec cscli metrics show acquisition parsers`. It shows lines read from `syslog` and parsed by `crowdsecurity/haproxy-logs`.

## Settings

In the dashboard, open **Settings** and select **CrowdSec**. The fields are **CrowdSec API URL**, **CrowdSec syslog address**, **Captcha provider**, **AppSec (WAF)** and **AppSec URL**. The keys have their own fields, as in step 3 above. A change to a field waits for **Stage changes** and **Apply**.

```yaml
# config.yaml
crowdsec:
  enabled: true
  lapi_url: http://127.0.0.1:18080
  syslog: 127.0.0.1:15140
```

`crowdsec.syslog` must be an IPv4 address and a port, such as `127.0.0.1:15140`. HomeProxy refuses a host name. HAProxy resolves its log targets only when it starts or reloads. If a name does not resolve then, HAProxy does not start, and every site goes down.

The dashboard never shows a key. It shows only whether the key is set. You can also put the keys in `secrets.yaml`. The apply fails while CrowdSec is on and the bouncer key is missing:

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

A key that you change in the dashboard reaches the bouncer at once. HomeProxy rewrites the config of the bouncer and restarts it, with no apply. A key that you change in the file reaches the bouncer at the next apply. Set it in the dashboard when nothing else is pending.

```yaml
# config.yaml
crowdsec:
  captcha_provider: turnstile   # none, turnstile, hcaptcha or recaptcha
  allowlist: [203.0.113.7, 2001:db8::/32]
```

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

## Ban scanners

Scanners send requests to the bare IP of your server, to a random host name, or with no host name. A real visitor uses the host name of a site. HomeProxy answers a request for a host that no route serves with `404 Not Found`, from the `be_unknown_host` backend.

The `homeproxy/unknown-host` scenario bans a client that sends two of these requests in about 10 minutes. With the default profile, the ban lasts 4 hours. One request does not cause a ban, because a person can type a wrong address one time. A browser can also ask for `/favicon.ico` after the page, so the scenario does not count that request.

These requests do not count:

- A request for a host that a route serves, on a path that no route of that host serves.
- A request for the host of a route that is switched off.
- A plain HTTP request. HomeProxy sends the client to HTTPS, and the scenario counts only the HTTPS request.
- A request through Cloudflare. CrowdSec never bans the addresses of Cloudflare, because of its `crowdsecurity/cdn-whitelist`.

A request for the host of a route that you deleted counts. If the scenario bans a visitor by mistake, delete the decision on the Security page. To stop these bans, remove the `crowdsec_unknown_host` lines from `compose.yaml`, then run `docker compose up -d crowdsec`.

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

- The list shows every active ban and captcha, with its address or range, its origin and its time left. Decisions from blocklists can number thousands, so the list hides them. The line below the list gives their number and the time of the newest blocklist update that brought decisions. **Show them** shows them, and a search finds them too. With 0 blocklist decisions, the line links to the CrowdSec console, where you enroll the engine to get blocklists. The dashboard cannot see whether the engine is registered with the Central API. To see that, run `docker compose exec crowdsec cscli capi status`. The origin is `crowdsec` for a local detection, `cscli` for a manual decision (yours or one from the dashboard) and `CAPI` or `lists` for a blocklist.
- A manual ban or captcha is for an IP address or a range, for at least one second.
- Deleting a decision takes effect within about 10 seconds, when the bouncer pulls its next update. An address from a blocklist can come back at the blocklist's next pull, so **Unban** on such an address asks first and offers **Allowlist and unban**, which lets it in for good.

`cscli decisions` in the CrowdSec container still works, as a fallback.

## Allowlist

CrowdSec never checks the addresses and ranges on the allowlist. HAProxy keeps the list itself, in `allow.lst`, so a decision cannot block them. To change the list, open **Security**. In **Always allowed**, enter an address or range and click **Allow**. To remove an entry, click its x button. In `config.yaml`, the list is `crowdsec.allowlist`.

An allowlist change from the dashboard takes effect at once, with no apply. HomeProxy saves it to `config.yaml`, changes the list in the running HAProxy and writes it to the live release, so it stays after a reload or a restart. An address is listed once, so `203.0.113.7` and `203.0.113.7/32` are the same entry. The change also adds a History entry, unless `/data/config` has a hand edit that is not applied yet (see [History and rollback](changes-and-apply.md#history-and-rollback)). Restoring an older snapshot brings back the allowlist it holds.

A range that reaches outside the private ranges, such as `198.51.0.0/16`, is added with a warning that names it, and every apply repeats the warning, because CrowdSec never checks any client in it. A single public address, such as one you allowlist from a ban, is no warning.

The dashboard refuses an allowlist change while a change to `config.yaml` is pending, for example a settings, domain or DNS change. Apply or discard that change first. Its copy of `config.yaml` holds the old allowlist. If you apply that copy, it undoes the allowlist change. Other pending changes, such as a route, do not stop an allowlist change.

If HAProxy does not answer, the change is still saved and the next apply uses it. Make the same change again to update HAProxy at once.

## AppSec (WAF)

The AppSec component of CrowdSec blocks known attacks, such as a probe for `/.env`, before the request reaches your service. It is off by default. To turn it on:

1. Change the engine container for AppSec. See [Engine container for AppSec](#engine-container-for-appsec).
2. In **Settings**, in the **CrowdSec** section, turn on **AppSec (WAF)**. Set **AppSec URL** only if you need it.
3. Click **Stage changes**, then click **Apply**.

In `config.yaml`:

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

### Engine container for AppSec

The bouncer looks for AppSec at `crowdsec.appsec_url`. Without it, the bouncer uses port 7422, the default of the AppSec component, on the host from `crowdsec.lapi_url`. The URL has a scheme, a host and an optional port, and no path. Change the `crowdsec` service from [Engine container](#engine-container) in three places, then run `docker compose up -d crowdsec`. The new `COLLECTIONS` line replaces the old line. It is not a second line:

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

## AppSec state

While AppSec is on, the CrowdSec section on the Settings page shows whether AppSec is up or down. The dashboard sends AppSec a request for a harmless page with the bouncer key, as the bouncer does. AppSec is up when it allows that request. AppSec fails open, so while it is down, requests pass without AppSec checks. The `appsec_down` notification tells you when AppSec is down for 2 minutes, and when it recovers.

## Connection test

**Test connection** in the CrowdSec section on the Settings page checks each part of CrowdSec and shows one line for each part. A part that fails does not stop the other checks. The test uses the applied settings and the saved keys.

| Line | What the test does |
| --- | --- |
| Machine | Logs in to the LAPI with the machine ID and the machine password |
| Bouncer | Asks the LAPI for decisions with the bouncer key |
| AppSec | Asks AppSec about a harmless request with the bouncer key. The line says "AppSec is off" while AppSec is off |
| Blocklist | Counts the active blocklist decisions with the machine login. 0 is a correct result: the engine can be registered, but have no blocklist decisions yet |

## Engine container

Add these parts to the `compose.yaml` from [Install HomeProxy](install.md):

- The `crowdsec` service under `services`.
- The `configs` block, as a new top-level key.
- The two `crowdsec-` volumes under the existing `volumes`.

To paste one file, for example into Komodo, use [`deploy/compose.crowdsec.yaml`](../deploy/compose.crowdsec.yaml). It holds the whole stack with AppSec on. Then skip the merges on this page. The additions are:

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
      - source: crowdsec_unknown_host
        target: /etc/crowdsec/scenarios/homeproxy-unknown-host.yaml
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
  crowdsec_unknown_host:
    content: |
      # HomeProxy answers a host that no route serves from its be_unknown_host backend. Real
      # visitors never ask for one, but scanners ask for the bare IP, a random name or no name.
      # The second such request in about 10 minutes bans the client. A browser can also ask for
      # /favicon.ico after the page, so that request does not count.
      type: leaky
      name: homeproxy/unknown-host
      description: "Ban a client that asks twice for a host that HomeProxy does not serve"
      filter: "evt.Meta.log_type == 'http_access-log' && evt.Parsed.backend_name == 'be_unknown_host' && evt.Meta.http_path != '/favicon.ico'"
      groupby: evt.Meta.source_ip
      capacity: 1
      leakspeed: 10m
      blackhole: 1m
      labels:
        service: http
        confidence: 3
        spoofable: 0
        classification:
          - attack.T1595
        behavior: "http:scan"
        label: "Request for an unknown host"
        remediation: true

volumes:
  homeproxy-data:
  crowdsec-config:
  crowdsec-data:
```

- `crowdsecurity/haproxy` brings the HAProxy log parser and the generic HTTP scenarios. The syslog acquisition receives HAProxy's access lines.
- `crowdsec_unknown_host` adds HomeProxy's own scenario, `homeproxy/unknown-host`. See [Ban scanners](#ban-scanners).
- HomeProxy runs on the host network, so the engine publishes its LAPI and its syslog acquisition (UDP) on `127.0.0.1` of the host, and HomeProxy uses those addresses.
- `configs` with `content` needs Docker Compose 2.23.1 or later. With an older Compose, put the acquisition in a file and mount it at the same path.
