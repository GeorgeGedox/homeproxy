# Internal DNS server

HomeProxy runs a small DNS server for the zones in your domains. It gives LAN clients the proxy's LAN address for each site. Thus LAN clients do not go out through the WAN address and back in. Your LAN resolver, for example AdGuard Home or Pi-hole, sends only your zones to it.

## Connect your LAN resolver

Set your LAN resolver to send each of your zones to the address in **DNS server address**. Keep its other upstreams.

The **DNS** page shows one line for each zone in two formats. Use the format that your resolver reads.

For AdGuard Home, click **Copy for AdGuard Home**. Paste the lines into the upstream DNS servers of AdGuard Home:

```text
[/example.com/]192.168.1.10:5335
```

For Pi-hole and other resolvers that use dnsmasq, click **Copy for dnsmasq or Pi-hole**. Add the lines to the dnsmasq configuration of the resolver:

```text
server=/example.com/192.168.1.10#5335
```

Do not put your LAN resolver in **DNS upstreams**. The resolver sends the zone to HomeProxy, and HomeProxy then sends the name back to the resolver, so the query loops. When a query comes from an address that is also in **DNS upstreams**, HomeProxy does not forward it. It answers SERVFAIL and logs a warning that names the address.

## The DNS page

The **DNS server** panel shows the state of the server:

| State | Meaning |
| --- | --- |
| Answering | The server answers, and it forwards other names in your zones |
| Answering, but only the names below | The server answers, but **DNS upstreams** is empty. Other names in your zones fail |
| Not answering | The server does not listen on its address |
| Not started yet | No apply has written the names yet |

The panel also shows the address of the server, your zones and the upstreams. A staged change to the upstreams shows as "Staged".

**Names it answers** lists each name and its address. Each site's hosts are in the list automatically. A staged name shows "staged", and a name that goes away shows "removed at the next apply". Click the name of a site to open the site.

### Add a record for a device

Use an extra record for a device that is not a site, for example a NAS or a printer.

1. Click **Add record**.
2. In **Name**, enter the name, for example `nas.example.com`. The name must be in one of your domains.
3. In **Address**, enter the IPv4 address of the device.
4. Click **Stage change**.
5. Click **Apply** in the bar at the bottom.

To remove an extra record, click the remove button (x) on its row. Then apply.

HomeProxy refuses an extra record for a name that is a host of an enabled site, because the site's answer wins. Set the DNS answer on the site instead. If you add a site for the name of an extra record later, the site wins.

### Change the answer for a site

The site editor has the field **On your network**:

- **HomeProxy**: LAN devices go through the proxy. This is the default.
- **Another address**: LAN devices go straight to the address that you enter.

A change that affects only DNS answers does not reload HAProxy.

## Settings

Open **Settings** and go to **DNS server**:

- **DNS server address**: the address and port where the server listens, for example `192.168.1.10:5335`. The default port is 5335, because mDNS uses 5353 and the LAN resolver usually uses 53. The server listens on UDP and TCP.
- **DNS upstreams**: the resolvers for the names in your zones that HomeProxy does not answer. Type an address and press Enter. Setup sets `1.1.1.1`, the public resolver that HomeProxy also uses to check certificate records. Thus the public records of your zones, for example `www` or the zone itself, resolve on the LAN.

Click **Stage changes**, then **Apply** in the bar at the bottom. In `config.yaml`, the settings are:

```yaml
dns_server:
  listen: 192.168.1.10:5335
  forward_unknown_to: [1.1.1.1, 9.9.9.9]
```

HomeProxy asks the upstreams in order. It asks the next upstream only when an upstream does not answer. If **DNS upstreams** is empty, all names in a zone that are not a site or an extra record get SERVFAIL. Then LAN clients cannot resolve the public records of your zones.

If the host does not have the address in **DNS server address**, the server logs `cannot listen on` and tries again every 30 seconds. This can occur at boot, before the LAN IP of the host is up.

## Another DNS server in the same zone

Another DNS server on the LAN can answer other names in one of your zones. An example is k8s_gateway for a Kubernetes cluster. Do not give your LAN resolver two upstreams for that zone. The resolver uses one answer, and an NXDOMAIN is an answer. Thus one server can hide the names of the other server.

Send the zone to HomeProxy only. Then do these steps in the dashboard:

1. Open **Settings** and go to **DNS server**.
2. In **DNS upstreams**, enter the other server first, for example `192.168.1.20`. Then enter `1.1.1.1`.
3. Click **Stage changes**.
4. Click **Apply** in the bar at the bottom.

HomeProxy answers for its sites and its extra records. It sends all other names in the zone to `192.168.1.20`. Make sure that the other server sends the names that it does not know to a public resolver. If it does not, the public records of the zone do not resolve on the LAN.

An NXDOMAIN is an answer, so HomeProxy does not ask the next upstream after it. Thus `1.1.1.1` answers only when the other server is down. HomeProxy sends queries to port 53 only.

A different LAN domain does not need HomeProxy. Send that domain from your LAN resolver straight to its own DNS server.

## Answers

| Query | Answer |
| --- | --- |
| `A` for a site host with the DNS answer **HomeProxy** | `network.haproxy_lan_ip`, TTL 60 seconds |
| `A` for a site host with **Another address** | That address |
| `A` for an extra record | Its address |
| Any other type for one of these names, such as `AAAA` | An empty answer (NOERROR), so clients do not use a public IPv6 address |
| Any other name in a zone | Forwarded to **DNS upstreams**, and the answer passed back unchanged. With no upstreams, SERVFAIL |
| A name that is not in one of your zones | REFUSED |
| A zone transfer (`AXFR`, `IXFR`) or a dynamic update | REFUSED |

A disabled site gets no local answer, so HomeProxy forwards its name like all other names. `tcp` routes have no host names and get no records.

The server answers only what the last successful apply wrote. A pending change has no effect on DNS until you apply it. The server uses the new answers less than 2 seconds after an apply.

## Limits

- Only private client addresses get an answer: `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, loopback, link-local and other ranges that the internet does not route. All other clients get no answer, also when **DNS server address** is `0.0.0.0` or a public address. Thus the internet cannot learn your LAN addresses. Tailscale and other `100.64.0.0/10` clients get no answer either: point them at your LAN resolver.
- Each client address can send 200 queries per second. Queries above the limit get no answer. Your LAN resolver counts as one client for all the LAN clients behind it, and its cache keeps it well below the limit.
- IPv4 only: there are no `AAAA` answers in this version.
- The DNS server is its own process. It continues to answer when the dashboard stops.
