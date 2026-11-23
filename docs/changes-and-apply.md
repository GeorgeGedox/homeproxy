# Pending changes, apply and history

Edits in the dashboard do not change HAProxy straight away. They collect as pending changes until you apply them, so you can review several edits and apply them together.

## Pending changes

Saving a route, a domain or a setting stages a copy of the changed file in `/data/pending`. `/data/config` stays as it is. A bar at the bottom of every page lists the staged changes in plain words, such as `Route app.example.com added` or `Network settings changed`, with **Details**, **Discard** and **Apply**. It shows only while something waits for an apply.

**Details** lists the changes, what Cloudflare will change, and two diffs folded below them:

- **Config:** the YAML files as they will be saved.
- **HAProxy:** the generated HAProxy files as the next apply will write them. If the pending config cannot be generated, for example because a route with basic auth has no users yet, Details shows the reason in place of this diff.

The diffs never show secrets. The dashboard password hash and every token or password HomeProxy knows are shown as `[REDACTED]`, and the generated file with the basic auth hashes is left out.

**Discard** asks first and names the changes it removes, then removes every pending change. `/data/config` and HAProxy are not touched.

Two things are saved at once and are not pending, because `secrets.yaml` has no pending copy:

- **Basic auth users.** They take effect at the next apply. The bar shows the change, such as `Basic auth users changed on wiki.example.com`, even with nothing else pending, and Discard does not undo it. Renaming a route copies its users to the new name, and deleting a route keeps them, so a discarded rename or delete leaves a route that still works. Each successful apply drops the users of routes that no longer exist.
- **A domain's API token.** It replaces the token stored under the domain's credentials name, which every domain with that name uses. So a new token is checked against every zone that uses that name, and refused if it cannot read one of them.

## Conflicts with hand edits

You can still edit the files in `/data/config` by hand. If you edit a file that has a pending change, the dashboard shows a conflict and names the file, and apply refuses to run, because applying would overwrite your hand edit. Discard the pending changes, then make the edits again in the dashboard.

A hand edit of a file with no pending change is not a conflict. The next apply loads it together with your pending changes. With nothing pending, the bar says the saved config differs from what runs: press **Apply** to load it. For example, after an apply fails on a broken include, you can fix or delete the include by hand and apply again. The HAProxy diff includes the hand edit, but the Config diff does not, because it shows only the pending files.

A route file that no longer parses after a hand edit can still be deleted in the dashboard.

## Apply

Apply checks the config, generates the HAProxy files, tests them with `haproxy -c`, loads them, and checks that HAProxy still answers. Only then are the pending changes saved to `/data/config`.

- If any step fails, the old config stays live, the changes stay pending, and the result names the step that failed (`validate`, `check`, `apply`, `verify` or `promote`) with the error. For `haproxy -c` and reload failures it also shows HAProxy's output.
- An upstream that is down is a warning, not a failure. The route is live and starts working when the upstream comes up.
- A public range in `network.lan_cidrs` or `crowdsec.allowlist` is a warning on every apply. See [Dashboard access](dashboard-access.md#settings).
- A Prometheus exporter address that this host does not have is a warning on every apply. The exporter answers once the address exists. See [Prometheus](prometheus.md).
- A change that only moves a host between LAN and public, or between routes, is loaded without a reload (the `map` path in the audit log). Any other change reloads HAProxy (`reload`), which keeps existing connections until they finish.
- An apply with nothing pending still runs. The bar offers it when the saved files differ from what HAProxy runs, and **Apply saved config** on History runs it at any time. It loads `/data/config` as it is, including basic auth users and hand edits, and checks that HAProxy answers. When nothing changed, it reloads the live release, so it also brings HAProxy back in line after a failed rollback.
- If HAProxy cannot bind a port, for example because another program holds the new HTTP port, the reload fails at the `apply` step with HAProxy's `Address already in use` message. The old release stays live, and the change stays pending.

After a successful apply, HomeProxy drops the basic auth users of removed routes and brings the certificates in line with the config:

- A domain or host that has no certificate yet gets one as a background job. HTTPS uses the generated default certificate until the job finishes.
- A certificate that no domain or host needs any more is removed, and HAProxy reloads.

## If the container stops during an apply

While an apply runs, `/data/generated.prev` points at the release that was live before it. If the container or the dashboard stops before the apply finishes, the next start switches `/data/generated` back to that release and removes the link, so HAProxy boots on a release that was already verified. The edits are still pending: apply again. The exception is a stop after the changes were already saved to `/data/config`. Then the new release stays live, because it matches the saved config.

## Domains

Saving a domain checks its Cloudflare token first: the token must be able to read the zone and its DNS records. A new token must also read every other zone that uses the same credentials name. The token also needs DNS edit permission, which only the certificate job can test. A domain cannot be removed while a route has a host in it that no other domain covers. A nested zone such as `sub.example.com` can be removed while `example.com` is configured, because its hosts then belong to `example.com`.

## History and rollback

Every successful apply that saves changes stores a snapshot of `/data/config`. The last 50 are kept. History lists them newest first, each with what its apply changed in the words the bar uses, such as "Route jellyfin.example.com added, Network settings changed". The newest is marked **Live now**. Snapshots from before this list existed name the routes and domains they changed by their internal names, and "Settings changed" for `config.yaml`. A long list shows the first three changes and how many more. Two more cases add a snapshot, so that the newest snapshot keeps matching the config HAProxy runs. The [Cloudflare reconcile](cloudflare-dns.md) changes records only while they match:

- An apply with nothing pending that loads a hand edit ("Saved files applied again"). **Apply saved files** at the bottom of History does this. It is hidden while changes are staged, because an apply would make those live too.
- A change that is saved at once: an allowlist entry and the dashboard address or session length ("Allowlist changed", "Dashboard address changed", "Session length changed"). If `/data/config` has a hand edit that is not applied yet, such a change adds no snapshot, so the hand edit stays unapplied until you apply it.

A restore of an older snapshot also brings back the allowlist it holds. The Config diff in Details shows it.

**Show changes** on an older snapshot shows what a restore would change, compared with the current config. **Restore** asks first, then stages that snapshot as pending changes, which show in the bar. It replaces anything staged before, and the dialog says how many changes that is. Apply them as usual. A restore never changes the dashboard password, address or session length. It fails if a domain in the snapshot uses credentials that no longer exist in `secrets.yaml`.

### Activity

The **Activity** tab on History lists the changes that are saved at once instead of staged, newest first, from the audit log: credentials replaced or deleted, keys set or cleared, notification targets, the dashboard address and session length, bans and unbans, always-allowed addresses, and Cloudflare adoptions and record removals. Each line names what changed, never a secret value: a key shows only its name. An allowlist entry and the dashboard address or session length show on both tabs: on **Applies** as the snapshot to restore, on **Activity** with the entry or value that changed.

## When the dashboard is busy

Only one change runs at a time. If an edit waits more than 10 seconds behind an apply, it is refused with "another change is in progress". Try again when the apply has finished.
