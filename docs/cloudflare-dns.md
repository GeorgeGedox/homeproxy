# Cloudflare DNS

With `cloudflare.enabled: true`, HomeProxy keeps two kinds of records in your Cloudflare zones:

- **Route records.** Each enabled, public `http` or `passthrough` route with `cloudflare: {dns: true}` gets a CNAME from each of its hosts to its domain's DDNS record. `tcp` routes have no host names, so they get no record.
- **The DDNS record.** For each domain with `ddns_update: true`, an A record on `ddns_record` (default `home.<zone>`) that holds your WAN address. HomeProxy checks the address at start, every 5 minutes, after every successful apply and when you select **Sync now** on the Cloudflare page.

With the integration off, HomeProxy makes no Cloudflare DNS call and leaves every record as it is. Certificates still use the token.

## Which records HomeProxy changes

HomeProxy marks the records it owns in the Cloudflare record comment:

| Record | Comment |
| --- | --- |
| Route record | `managed-by:homeproxy route:<route name>` |
| DDNS record | `managed-by:homeproxy ddns:<zone>` |

HomeProxy changes or deletes only records with the exact marker. A record you made by hand is never touched, even when a route uses the same name. The Cloudflare page shows that host as a conflict. To let HomeProxy manage the name, delete your record in Cloudflare and select **Sync now**.

Do not edit these comments by hand. A record whose comment no longer matches counts as hand-made.

## When records change

Reconcile brings the route records in line with the applied routes. It runs:

- after every successful apply, as a background job,
- every `cloudflare.reconcile_interval`, 15 minutes by default,
- when you select **Sync now** on the Cloudflare page.

To change the interval, set **Sync interval** in the Cloudflare section of Settings (or `reconcile_interval` in the `cloudflare` section of `config.yaml`), such as `30m` or `1h`, stage it and apply. The new interval starts at that apply. It must be at least `5m`. A reconcile makes about two Cloudflare calls per zone, far under the API's limit, but routes change only at an apply, which reconciles at once. The scheduled run repairs records that someone changed in Cloudflare and retries a reconcile that failed.

The apply does not wait for Cloudflare. Its result names the reconcile job, and the job shows whether the records were updated. The apply also starts a DDNS update, so a new domain gets its DDNS record without waiting for the 5-minute check. A DDNS update that fails never fails the apply either. A reconcile that fails never fails the apply: HAProxy keeps the new config, and the job ends failed with the Cloudflare problem. Each zone runs on its own, so one failing zone does not stop the others. Only one reconcile runs at a time. Another one waits for it to finish.

The Cloudflare page shows, for each zone, the DDNS record and the site records HomeProxy owns, with what the next apply or sync will add, change or remove, from your pending changes. Showing it writes nothing. A light at the top says whether the records are in sync, when they last synced and when DDNS last checked your address.

### When reconcile refuses to run

Reconcile changes nothing and says why when:

- **A config file does not load.** A broken route file must never look like a deleted route, so reconcile deletes nothing until every file loads.
- **`/data/config` has hand edits that are not applied.** Records follow only what HAProxy serves. Apply the edits with Apply in the bar at the bottom, or undo them, and reconcile runs again.

## Remove site records

**Remove site records** on the Cloudflare page deletes every route record HomeProxy owns, in every zone, even while the integration is off. It shows while any domain's DNS is on Cloudflare. It keeps the DDNS record, because records you made by hand may point at it. Delete the DDNS record in Cloudflare yourself if you no longer need it.

Turn the integration off first, or the next reconcile creates the route records again. The audit log records each removal, with the names deleted.

## Adopting a hand-made DDNS record

If `ddns_record` already has one A record that you made, HomeProxy does not change it. The Cloudflare page shows it as hand-made with an **Adopt** button. Adopting writes the DDNS marker into the record's comment and keeps its address, proxy setting and TTL. HomeProxy then runs a DDNS update at once, which moves the record to your WAN address. The audit log records the adoption.

The page shows no Adopt button when the name holds records that adoption cannot fix: a CNAME, more than one hand-made A record, or a HomeProxy record next to a hand-made one. Clean up the records in Cloudflare until one A record is left.

## Things to know

- **Renaming `ddns_record`** creates a new DDNS record on the new name. The old owned record stays in Cloudflare. Delete it there yourself once nothing points at it.
- **A nested domain that is not its own Cloudflare zone.** You can add `sub.example.com` as a domain only for its wildcard certificate while its records live in the `example.com` zone. Set `ddns_update: false` on it, because HomeProxy cannot find a Cloudflare zone for it and its DDNS update would fail every 5 minutes. Reconcile reports an error for the nested domain and creates no records for its hosts, so make those records in `example.com` by hand. It never deletes a HomeProxy record on such a host in `example.com`.
- HomeProxy creates DDNS records DNS-only with automatic TTL. If you change the proxy setting or TTL of an owned DDNS record in Cloudflare, HomeProxy keeps your values.
- Every Cloudflare call is logged with its method, path and status. The token is never logged. When Cloudflare rate-limits a call, HomeProxy waits and tries again.
