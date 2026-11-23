# Internal DNS server

HomeProxy runs a small DNS server for the zones in `domains`. LAN clients get the proxy's LAN address for every route, so they never go out through the WAN address and back in. Point AdGuard Home, or another LAN resolver, at it for your zones only.

## Answers

| Query | Answer |
| --- | --- |
| `A` for a route host with `dns: {target: haproxy}` | `network.haproxy_lan_ip`, TTL 60 seconds |
| `A` for a route host with `dns: {target: <ip>}` | That address |
| `A` for a name in `dns_records` | Its `target` |
| Any other type for one of these names, such as `AAAA` | An empty answer (NOERROR), so clients do not use a public IPv6 address |
| Any other name in a zone | Forwarded to `dns_server.forward_unknown_to`, and the answer passed back unchanged. With an empty list, SERVFAIL |
| A name outside every zone | REFUSED |
| A zone transfer (`AXFR`, `IXFR`) or a dynamic update | REFUSED |

A disabled route gets no local answer, so its name is forwarded like any other name. `tcp` routes have no host names and get no records.

The DNS page shows whether the server answers, the AdGuard upstream lines to copy for every zone, and every name it answers: each site's hosts and the extra records in `dns_records`, marked as live, staged, or removed at the next apply. It adds and removes the extra records. An extra record must be inside a configured domain. A name that an enabled route has as a host is refused, because the route's answer would win: set the DNS target on the route instead. If you later add a route for the name of an extra record, the route wins. Like other edits, it waits in the pending changes until you apply.

The server answers only what the last successful apply wrote. A pending change has no effect on DNS until you apply it. New answers are served within 2 seconds of an apply. A change that affects only DNS answers, such as a route's DNS target, does not reload HAProxy.

## Settings

```yaml
dns_server:
  listen: 192.168.1.10:5335
  forward_unknown_to: [1.1.1.1, 9.9.9.9]
```

The default port is 5335, because mDNS uses 5353 and AdGuard usually has 53. The server listens on UDP and TCP.

If the host does not have the `listen` address, for example while its LAN IP is not up yet at boot, the server logs `cannot listen on` and tries again every 30 seconds.

Setup writes `forward_unknown_to: [1.1.1.1]`, the public resolver HomeProxy already uses to check certificate records, so the public records of your zones, such as `www` or the zone itself, keep resolving on the LAN. The upstreams are asked in order, and the next one is tried when one fails. If you empty the list, every name in a zone that is not a route or an extra record answers SERVFAIL, and LAN clients cannot resolve those public records.

## AdGuard Home

Add one upstream line per zone in AdGuard Home's upstream DNS servers:

```text
[/example.com/]192.168.1.10:5335
```

Do not put AdGuard Home in `forward_unknown_to`. AdGuard sends the zone to HomeProxy, so HomeProxy would send the name back to AdGuard, and the query would loop. When a query comes from an address that is also in `forward_unknown_to`, HomeProxy does not forward it. It answers SERVFAIL and logs a warning that names the address.

## Limits

- Only private client addresses get an answer: `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, loopback, link-local and other ranges the internet does not route. Any other client gets no answer, even when `listen` is `0.0.0.0` or a public address, so the internet cannot learn your LAN addresses. Tailscale and other `100.64.0.0/10` clients get no answer either: point them at AdGuard on the LAN.
- Each client address may send 200 queries per second. Queries above the limit get no answer. AdGuard counts as one client for all the LAN clients behind it, and its cache keeps it well below the limit.
- IPv4 only: there are no `AAAA` answers in this version.
- The DNS server is its own process. It keeps answering when the dashboard stops.
