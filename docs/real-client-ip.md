# Real client IP behind Cloudflare

When Cloudflare proxies a route, every connection comes from a Cloudflare edge address. HomeProxy replaces that address with the visitor's, so GeoIP, CrowdSec, the access log and the `X-Forwarded-For` header your upstream gets all show the real client. This holds only for a route that counts as proxied, as [below](#proxied-routes-cannot-be-bypassed).

## How the client address is found

| Request comes from | Client address | `CF-Connecting-IP` reaches the upstream |
| --- | --- | --- |
| A Cloudflare edge address, on a proxied route | The value of `CF-Connecting-IP`. It can be IPv6. A value that is not one address leaves the edge address | Yes |
| A Cloudflare edge address, on any other route | The edge address. A `CF-Connecting-IP` header is ignored | No, it is removed |
| Anywhere else | The connection's own address. A `CF-Connecting-IP` header is ignored | No, it is removed |

A Cloudflare Worker on any account can send any `CF-Connecting-IP` to a host it reaches directly, from Cloudflare's addresses. So the header names the client only on a route your zone proxies. If you turn on Cloudflare's proxy for a record by hand, with HomeProxy's Cloudflare DNS off for the route, HomeProxy sees Cloudflare's edge as the client: GeoIP, CrowdSec and the log then act on edge addresses. Let HomeProxy manage the record instead.

Nobody can pretend to be another client by sending `CF-Connecting-IP` directly to your IP. The same holds for the spelling with underscores, `CF_Connecting_IP`, which some upstreams read as the same header. HomeProxy also removes `X-Forwarded-For`, `X-Forwarded-Host`, `X-Forwarded-Port`, `X-Forwarded-Proto`, `X-Real-IP` and `Forwarded` that a client sends, in both spellings, and then adds its own `X-Forwarded-For` and `X-Forwarded-Proto`.

A LAN-only route always refuses a request that comes through Cloudflare, even when the header names a LAN address: a LAN-only route is never proxied, so the header is ignored there.

## Proxied routes cannot be bypassed

A public route whose Cloudflare record is proxied accepts only:

- requests through Cloudflare, and
- requests from `network.lan_cidrs`, so LAN clients can still connect directly.

Anyone else who connects to your WAN address with that host name gets a plain 403.

A route counts as proxied when all of these hold:

- `cloudflare.enabled` is `true` in `config.yaml`.
- The route is `public` and has `cloudflare: {dns: true}`.
- It sets `proxied: true`, or leaves `proxied` out and every host is in a zone with `default_proxied: true`.
- No host is the zone's DDNS record (`ddns_record`, by default `home.<zone>`). That record is never proxied, so a route that uses it accepts direct connections.

A route with Cloudflare DNS off, or with a DNS-only record, accepts direct connections as before.

## Cloudflare's address list

HomeProxy ships a copy of Cloudflare's published edge ranges, IPv4 and IPv6. The dashboard downloads the current lists from `https://www.cloudflare.com/ips-v4` and `https://www.cloudflare.com/ips-v6` when it starts and once a day after that. A new list takes effect in HAProxy at once, without a reload, and every later apply uses it.

If a download fails, or a list holds anything but public addresses and ranges, nothing changes and the job reports the error. HAProxy keeps the last good list. If HAProxy cannot take a new list, the next run tries again. The job shows as `cloudflare.ranges` in the job list.
