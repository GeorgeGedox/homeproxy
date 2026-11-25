# Route types

To add one, open **Routes** and click **Add site**. The editor asks, in order:

1. **Where:** the type (Website, Redirect, TLS passthrough or TCP port), the hosts or the port, and for a website the path.
2. **To:** the upstreams as `host:port`, or the target URL of a redirect. You can paste a URL such as `https://192.168.1.20:8443/login`: the editor keeps `192.168.1.20:8443` and turns on **Connect over HTTPS**.
3. **Site:** LAN only or Public, what the internal DNS server answers on your network, and the Cloudflare DNS record and proxy when the domain is on Cloudflare. These belong to the host, so a change here applies to every path of the site, and the editor names the paths it also updates. A TCP port has only **Who can reach it**.
4. **Protection:** for a website an optional password, and for any route a list of blocked countries, which you can switch off without losing it.
5. **Advanced**, folded, websites only: stripping the path, WebSocket, the upstream certificate check and headers.

**Stage change** puts the route in the pending changes; **Apply** in the bar makes it live. To send a path of an existing site to another service, click **Add path** under the site.

Each route also has a `name`, its internal ID. It is the file name, `routes/<name>.yaml`, and the label in logs, metrics and alerts; the editor shows it next to **Stage change**. HomeProxy gives a new route its name from the first host and path, such as `app-example-com` or `example-com-api`, or `tcp-25565` for a `tcp` route. When a route already has that name, a number follows: `app-example-com-2`. The name stays the same when you change the hosts later.

The files below are the reference for each type.

| Type | Use it for | How HAProxy handles it | What applies |
| --- | --- | --- | --- |
| `http` (Website) | Web apps, such as `jellyfin.example.com` | Ends TLS with your certificate and routes by the Host header and path | Everything: LAN-only, GeoIP, CrowdSec, request and response headers, auth, Cloudflare proxy |
| `passthrough` (TLS passthrough) | A service that keeps its own certificate, such as Proxmox on `pve.example.com` | Reads the host name from the TLS handshake (SNI) on the HTTPS port and forwards the encrypted stream | LAN-only, GeoIP and CrowdSec bans, by the client's address |
| `tcp` (TCP port) | Anything else on its own port, such as Minecraft on 25565 or SSH on 2222 | Listens on `listen_port` and forwards raw TCP | LAN-only, GeoIP and CrowdSec bans, by the client's address |
| `redirect` | Sending one host to another, such as `www.example.com` to `example.com` | Answers every request on its hosts with a redirect, over HTTPS and plain HTTP | LAN-only, GeoIP, CrowdSec, Cloudflare proxy. Certificates, internal DNS and Cloudflare records as for `http` |

## Domains without a certificate

A domain with no `dns_provider` and no `credentials` gets no certificate:

```yaml
# config.yaml
domains:
  - zone: example.net
```

`http` and `redirect` routes on such a domain answer on the HTTP port only:

- No redirect to HTTPS and no HSTS header. The upstream gets `X-Forwarded-Proto: http`.
- Every check still applies, as on HTTPS: LAN-only, GeoIP, CrowdSec, basic auth, paths, and the `400` for a path with a `.` or `..` segment or one that does not decode.
- On the HTTPS port, the host is unknown: HAProxy shows its self-signed default certificate and answers `404`. One exception: when the zone is one label below a zone with a wildcard certificate, such as `lab.example.com` under `example.com`, the apex `lab.example.com` gets that wildcard certificate, then the same `404`.
- A route cannot have hosts in a domain with a certificate and in one without. Apply refuses it.
- `passthrough` and `tcp` routes work as on any other domain. Cloudflare DNS records and DDNS need the Cloudflare provider, so such a domain has none. Apply refuses `cloudflare.dns: true` on a host in any domain that is not on Cloudflare.

**Warning:** Basic auth over plain HTTP sends the user name and password in clear text. Anyone on the network path can read them. Use it on such a domain only on a network you trust.

## Paths on `http` routes

Several `http` routes can share a host when each one serves its own path. On **Routes**, click **Add path** under the site and enter the path, such as `/api`.

```yaml
# routes/app-api.yaml
version: 1
name: app-api
type: http
hosts: [app.example.com]
exposure: public
upstreams:
  - address: 192.168.1.21:8080
path_prefix: /api
strip_prefix: true
```

- `path_prefix` is `/` (the default) or whole path segments, such as `/api` or `/api/v2`, without a `/` at the end. Segments hold letters, digits, `.`, `_` and `-`. A host and path pair belongs to one route only.
- A prefix matches whole segments: `/api` matches `/api` and `/api/x`, not `/apiv2`. When several prefixes match, the longest wins, so `/api/v2/x` goes to a `/api/v2` route before a `/api` route.
- A host with routes on paths but no `/` route answers `404` outside those paths. On the LAN that holds even when the host has a public site: the [internal DNS server](internal-dns.md) answers the whole name with HAProxy, so `https://example.com/` gets `404` there while it works outside. A DNS target cannot help: it sets the answer for the whole host, so pointing it at the public server takes the path routes off the LAN. The editor, the save and the apply warn about such a host. Put the routes on a separate host, such as `app.example.com`, or add a route for `/` whose upstream is the public server.
- With `strip_prefix: true`, the upstream sees the path without the prefix: `/api/x?q=1` reaches it as `/x?q=1`, and `/api` as `/`. The upstream must then build its links without the prefix, or with one it is told about in its own settings.
- On a host with routes on paths, HomeProxy decodes percent-encoding, drops `;` parameters and counts repeated `/` as one before it matches, so `/%61pi`, `/api;x` and `//api` reach the `/api` route. The upstream still gets the path as sent. A path that does not decode, such as `/api/%zz` or one with `%00`, gets `400`, and so does a path with a `.` or `..` segment. Browsers never send these. On a route that strips its prefix, a path that spells the prefix in another way, such as `/%61pi/x`, also gets `400`.
- Matching is case-sensitive: `/API` does not match `/api`. A path route is therefore not an access boundary for an upstream that ignores case, such as an ASP.NET app like Jellyfin, or that reads `\` as `/`, such as IIS. Do not use `auth` or `geoip` on a path route to protect part of such an upstream. Put that upstream on its own host instead.
- Routes on one host must agree on `exposure`, `cloudflare` and `dns`, because those belong to the host. Saving a route sets them on every route of its site, so the site never splits. Apply names the host when files edited by hand differ. `auth`, `request_headers`, `response_headers`, `geoip` and upstreams can differ per route.
- `redirect` and `passthrough` routes own their hosts alone.

## Headers on `http` routes

An `http` route can set headers in both directions, under **Advanced** in the editor:

```yaml
# routes/jellyfin.yaml
request_headers:             # sent to the upstream with each request
  X-Forwarded-Prefix: /media
response_headers:            # sent to the client with each response
  Content-Security-Policy: "default-src 'self'"
```

- A header replaces one of the same name that the client or the upstream sent.
- Values are printable ASCII and are sent exactly as written. Quotes are fine, and `%[...]` is not an HAProxy expression.
- A name is set once: `X-A` and `x-a` on the same route are refused.
- Neither direction can set `Connection`, `Content-Length`, `Keep-Alive`, `Proxy-Connection`, `TE`, `Trailer`, `Transfer-Encoding` or `Upgrade`, which HAProxy manages.
- A request header cannot be `X-Forwarded-For`, `X-Forwarded-Proto` or `CF-Connecting-IP`, which carry the real client to the upstream. See [Real client IP](real-client-ip.md).
- A response header cannot be `Host` or `CF-Connecting-IP`, which mean something only on a request.

## Sticky upstream

A route can have more than one upstream. HAProxy normally sends each new connection to the next upstream in turn. Some apps keep a session on one node, such as the Proxmox console. These apps fail when a request of one client goes to another node.

To keep each client on one upstream, open the route, go to **To**, and turn on **Sticky upstream**. It is off by default. The switch is there for `http`, `passthrough` and `tcp` routes that have more than one upstream.

```yaml
# routes/pve.yaml
upstreams:
  - address: 192.168.1.20:8006
  - address: 192.168.1.21:8006
sticky: true                 # default false
```

- HAProxy picks the upstream from the address of the client (`balance source`). A client stays on its upstream while that upstream is up. If it goes down, the client moves to another upstream. When it comes back, some clients can move back.
- All clients that share one address, such as clients behind one router, use the same upstream. The load can then be uneven.
- On a route that Cloudflare proxies, HAProxy uses the address of the visitor, not the address of Cloudflare. See [Real client IP behind Cloudflare](real-client-ip.md).

## `redirect` routes

In the editor, pick **Redirect**, enter the hosts and the target URL, and choose the kind of redirect.

```yaml
# routes/www.yaml
version: 1
name: www
type: redirect
hosts: [www.example.com]
exposure: public
redirect: {to: "https://example.com", code: 301, keep_path: true}
```

- `to` is an absolute `http://` or `https://` URL with a lowercase host. It may have a path and a query, but no spaces, quotes, `#`, `[` or `|`.
- `code` is `301` (the default) or `308` for a permanent move, `302` or `307` for a temporary one. `307` and `308` keep the request method and body.
- With `keep_path: true`, the request's path and query go after `to`: `https://www.example.com/a?b=1` goes to `https://example.com/a?b=1`. A `/` at the end of `to` is dropped, so the path is not doubled. `to` cannot have a query then, and saving one is refused. With `keep_path: false` (the default), every request goes to `to` exactly.
- Plain HTTP requests on a redirect host go straight to `to`, not to HTTPS first. They pass the same checks as HTTPS requests: a LAN-only redirect gives `403` to a client outside the LAN, and a blocked country or a CrowdSec ban is refused.
- A redirect route has no upstreams, so the status page does not list it. The routes list shows it with its hosts.
- Changing only `to`, `code` or `keep_path` needs no reload.
- `to` cannot point at one of the route's own hosts, because every request would redirect again. Saving such a route fails on `redirect.to`.

## `passthrough` routes

In the editor, pick **TLS passthrough**, enter the hosts and the service's address.

```yaml
# routes/pve.yaml
version: 1
name: pve
type: passthrough
hosts: [pve.example.com]
exposure: lan
upstreams:
  - address: 192.168.1.25:8006
```

- The browser sees the backend's own certificate. HomeProxy issues none for these hosts.
- While at least one `passthrough` route exists, a small TCP layer owns the HTTPS port. It sends passthrough hosts to their backends, and every other connection on to the normal HTTPS frontend. The client's address goes with it, so LAN-only checks, GeoIP, CrowdSec and the access log still see the real client. With no `passthrough` route, this layer is not there.
- A browser can reuse one connection for every host that a certificate covers, such as a wildcard. A request for a passthrough host on a connection that HomeProxy ended gets `421 Misdirected Request`, and the browser connects again, this time to the backend.
- A refused client (LAN-only, blocked country or CrowdSec ban) gets its connection closed. TLS cannot show a page before the backend answers.
- Leave Cloudflare proxying off. Cloudflare would end TLS itself, and HomeProxy would only see Cloudflare's address.

## `tcp` routes

In the editor, pick **TCP port**, enter the port HomeProxy listens on and the service's address.

```yaml
# routes/minecraft.yaml
version: 1
name: minecraft
type: tcp
listen_port: 25565
exposure: public
upstreams:
  - address: 192.168.1.40:25565
```

- Forward the port on your router yourself. A `tcp` route gets no DNS record, because it has no host name.
- `listen_port` must not be the HTTP or HTTPS port, the dashboard's or DNS server's port, or another `tcp` route's port. Apply refuses a clash.
- UDP is not supported, so game servers that use UDP, WireGuard and DNS cannot be routes.

## Both types

- HAProxy checks each upstream by opening a TCP connection every 10 seconds. The route shows `DOWN` when that fails.
- An idle connection stays open for up to one hour. After a reload, such as most applies, connections that were open before it close within 30 minutes.
- Each connection gets one access line in HAProxy's TCP log format, with the route, the country and, for `passthrough`, the host name from the handshake.
