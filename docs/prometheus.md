# Prometheus exporter

HAProxy has a built-in Prometheus exporter. HomeProxy can turn it on. Then Prometheus or Grafana scrapes HAProxy directly, without the dashboard.

## Settings

To turn on the exporter:

1. Open **Settings** and select **Prometheus**.
2. Turn on **Prometheus exporter**.
3. Enter **Prometheus listen address**, for example `192.168.1.10:8405`.
4. Click **Stage changes**, then click **Apply**.

For a hand edit, use `config.yaml`:

```yaml
prometheus:
  enabled: true
  listen: 192.168.1.10:8405
```

- `listen` is an IPv4 address and a port. It is required while `enabled` is true.
- HAProxy listens only on that address. Use the proxy's LAN IP.
- If the host does not have that address, for example after its LAN IP changed, HAProxy still starts and proxies. The exporter answers once the address is back. Every apply warns while the host lacks the address, which also catches a typo.
- The port must not be one that HAProxy or the dashboard already uses: `network.http_port`, `network.https_port`, the `listen_port` of a `tcp` route, the dashboard's port or the DNS server's port. HomeProxy refuses to save a clashing port, and it names the port. An apply checks the rule again.

## Scraping

Point Prometheus at `http://192.168.1.10:8405/metrics`:

```yaml
scrape_configs:
  - job_name: homeproxy
    static_configs:
      - targets: [192.168.1.10:8405]
```

- Only clients in `network.lan_cidrs` get the metrics. Any other client gets status 403.
- Every other path gets status 404.
- Scrapes are not logged, so they do not appear in the log tail or the metrics charts.

The metrics are HAProxy's own counters per frontend, backend and server. Each route's backend is named `route-<name>`. Requests by country come only from the access logs, so they are on the dashboard's Metrics page, not in the exporter.
