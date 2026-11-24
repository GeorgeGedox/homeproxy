# GeoIP country blocking

HomeProxy blocks countries in HAProxy, and shows the country of every request in the access log and metrics. The two use the same data in different places: HAProxy holds only the countries you block, and the dashboard looks up every request's country itself.

## In the dashboard

- **Security, Countries card:** pick the countries blocked on every public site. The card shows how much data is loaded, from which provider and how old it is, with the credit its license asks for. **Refresh data** downloads it now and shows the download until it ends, or its error.
- **A site's settings, Protection:** **Block countries** adds countries for that site only.

Each change is staged and takes effect at the next apply.

## Settings

```yaml
# config.yaml
geoip:
  provider: dbip-lite        # or maxmind
  block_countries: [CN, RU]  # blocked on every public route
```

```yaml
# routes/jellyfin.yaml
geoip: {block_countries: [US]}  # blocked on this route, on top of the global list
```

Country codes are the two-letter codes of the GeoIP data, such as `DE`. Most are ISO 3166 codes, but DB-IP and MaxMind also use `XK` for Kosovo. A lowercase code is saved in capitals. A save refuses a code that the data does not have, and refuses any new code before the first download, with the error of the last download when it failed. Codes already in a list never block a save, so a list can always shrink. Block lists change HAProxy only through an apply, which reloads it with the ranges of the countries now blocked. An apply does not fail over a code the data does not have, or over a block list with no data, such as one written by hand in `config.yaml`. It warns that those countries are not blocked.

## What a block does

- A request from a blocked country gets a plain 403 on a public route.
- Clients in `network.lan_cidrs` are never blocked.
- A route with exposure `lan` already refuses every client from outside the LAN, so its own list has no effect.
- `geoip: {enabled: false, block_countries: [US]}` keeps the route's list but blocks nothing on that route. The global list still applies to it, and its countries are not loaded into HAProxy's map.
- On a route Cloudflare proxies, the country is the visitor's, never the one of Cloudflare's edge (see [Real client IP behind Cloudflare](real-client-ip.md)). On any other route, a request through Cloudflare gets the country of Cloudflare's address. Visitors with IPv6 addresses are looked up too.
- An address with no country, such as a private or reserved range, is never blocked and has an empty country in the logs.
- Requests from countries you do not block are logged and counted with their country too. A blocked request is logged with the country it was blocked for.
- For IPv6, blocking works per /32: each /32 takes the country of its first address. A visitor from a smaller network of a blocked country inside an unblocked /32 gets through, and the logs show the blocked country.
- `tcp` and `passthrough` routes are blocked too. They cannot show a page, so HAProxy closes the connection. A `passthrough` route that Cloudflare proxies sees only Cloudflare's edge, so the country is the edge's.

## Data

| `provider` | Source | Needs |
| --- | --- | --- |
| `dbip-lite` (default) | DB-IP IP to Country Lite, CC BY 4.0 | Nothing. The dashboard must show the credit "IP Geolocation by DB-IP" with a link to https://db-ip.com |
| `maxmind` | MaxMind GeoLite2 Country | A free license key in `secrets.yaml` under `geoip.maxmind_license_key`, or set on the Settings page, which never shows it |

The dashboard downloads the data right after setup, and again when the data is 30 days old. A restart does not delay the next download. The download is a background job (`geoip.refresh`). A failed download keeps the data in use and is tried again an hour later. The scheduled download sends one `geoip_failed` message when it starts failing, and no more until it works again. A download you start with **Refresh data** sends one on each failure.

New data goes live at once, through a reload, when no changes are pending. When changes are pending, the new data waits for your next apply, because a background job never applies your pending changes. If the apply of new data fails, the old data comes back, so it can never block your own applies. Note that this apply also loads any hand edits in `/data/config` that you did not apply yet.

The data lives in `/data/geoip/`: `country.map`, `country-lookup.bin` for the logs, and `country.json`, which records the provider, the time of the download and the country codes in the data. A `country.json` from an older version has no codes, so it counts as no data until the next download, about a minute after start. The logs use new data within 10 seconds of a download, even while the map waits for your next apply.

## Accuracy and memory

HAProxy keeps its map in memory, about 300 bytes for each entry. It loads only the ranges of the countries blocked globally or on a public route whose list is on. With no country blocked, its map is empty. The full DB-IP data, written as plain address blocks, would need about 1.4 million entries and more than 400 MB. HomeProxy makes the map smaller in two ways:

- A wide block carries the most common country of its range, and narrower blocks carry only the exceptions. This loses nothing.
- IPv6 addresses are looked up per /32, the smallest block that registries give to an ISP. Each /32 gets the country of its first address. A few IPv6 visitors can get the country of a neighbouring network.

With the data of November 2026, the whole map has about 393,000 entries, which would take about 115 MB in HAProxy. Blocking one country loads far less: about 102,000 entries for US, 41,000 for DE and 11,000 for CN. A map of more than 450,000 entries is refused, and the old data stays in use, so the container stays under 300 MB even when you block almost every country.

The dashboard finds the country of each logged request with a binary search over the whole data, in about a microsecond, and keeps the data in about 6 MB. It resolves IPv6 addresses per /64, so the logs can be more precise than blocking.

The map is built in a separate process, which uses about 140 MB for a few seconds and then exits. The dashboard keeps none of that memory.
