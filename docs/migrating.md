# Migrating from a hand-written HAProxy config

You can run HomeProxy next to your current HAProxy, test every route on spare ports, and move to ports 80 and 443 only when all of them work. Nothing changes for your users until the last step.

HomeProxy does not read your `haproxy.cfg`. You recreate each frontend rule as a route. The table below maps what you wrote to what to enter.

## 1. Start HomeProxy on spare ports

Follow [Install](install.md) up to step 4, with these lines in `.env`. Pick ports that nothing uses, for example:

```sh
HOMEPROXY_BOOTSTRAP_LISTEN=192.168.1.10:8443
HOMEPROXY_PLACEHOLDER_HTTP_PORT=8081
```

Finish the setup wizard. It sees that the old HAProxy listens on ports 80 and 443, and asks for the HTTP and HTTPS ports. Enter `8081` and `8444`. The dashboard port (`8443` here), the DNS server port and the two HAProxy ports must all differ, or the first apply fails and Overview shows it.

HAProxy listens on every address of the machine on these ports, so it can run beside the old one, which holds ports 80 and 443. The internal DNS server listens on `dns_server.listen`, port 5335 by default. It does not touch your LAN resolver until you point it there.

Add your domain (step 7 of the install). The wildcard certificate is issued with a DNS challenge, so it works while the spare ports are the only ones HomeProxy uses.

## 2. Recreate the routes

| In your `haproxy.cfg` | In HomeProxy |
| --- | --- |
| `use_backend` chosen by `hdr(host)` in a `mode http` frontend | An `http` route with those hosts |
| `server` lines of that backend | The route's upstreams, one `address` each |
| `option httpchk`, `check` | The route's health check (path and interval) |
| `ssl verify none` or `ssl verify required` on a `server` | Upstream TLS on, with verify off or on |
| `acl lan src ...` with `http-request deny` | Exposure `lan`. HomeProxy returns 403 to a source outside `network.lan_cidrs` |
| `userlist` with `http-request auth` | Auth `basic`. Enter each user and a new password. See [Basic auth](basic-auth.md) |
| `http-request set-header`, `http-response set-header` | The route's `request_headers` and `response_headers` |
| `mode tcp` frontend on its own port | A `tcp` route with that listen port |
| `req.ssl_sni` rules that pass TLS through | A `passthrough` route with the host |
| Anything else | A file `/data/config/includes/<name>.cfg`. HAProxy loads it after the generated files, and apply checks it with `haproxy -c` |

HomeProxy handles these itself, so leave them out: the HTTP to HTTPS redirect, HSTS, certificate files and `crt` lines, timeouts, and the real client address on routes Cloudflare proxies. HomeProxy trusts `CF-Connecting-IP` only on those routes, so let HomeProxy manage their Cloudflare records (see [Real client IP behind Cloudflare](real-client-ip.md)).

Review the routes and apply, as in [Pending changes, apply and history](changes-and-apply.md). Overview shows a route as up when its upstream answers.

## 3. Test with Host headers

From a LAN machine, call each route on the spare HTTPS port without changing DNS:

```sh
curl --resolve app.example.com:8444:192.168.1.10 https://app.example.com:8444/
```

Check the status code, the certificate, and for a LAN-only route a `403` from a source outside the LAN ranges. Compare with the same request to the old HAProxy.

## 4. Move to ports 80 and 443

1. Stop the old HAProxy. The ports must be free before HomeProxy can bind them.
2. In Settings, set the HTTP port to `80` and the HTTPS port to `443`, then apply.
3. If apply fails with `HAProxy refused the new release` and `Address already in use`, something still holds the port. The old release stays live on the spare ports, and the change stays pending. Find the program with `ss -ltnp` and stop it, then apply again.
4. If the old HAProxy ran on another machine, change the router's port forward to `192.168.1.10`. If it ran on this machine, nothing in the router changes.
5. Point the LAN resolver at the HomeProxy DNS server for your zones ([Internal DNS server](internal-dns.md)).

Remove the line `HOMEPROXY_PLACEHOLDER_HTTP_PORT` from `.env` when you like. It has no effect once a config exists.

## Going back

Until you delete the old config, you can go back: set the ports in Settings to the spare ones, apply, then start the old HAProxy. Or run `docker compose stop` in the HomeProxy directory and start the old HAProxy.
