# Overview, jobs and the audit log

## Overview

Overview is the first page after login. It answers "is anything wrong?" first, then shows the traffic. It reads the status from HAProxy every 10 seconds while it is open.

| Block | Shows |
| --- | --- |
| Status strip | HAProxy with its version, uptime, a newer patch of its branch and the end of that LTS branch. The internal DNS server, and how long it has been up. The CrowdSec bouncer, `up` or `down` as HAProxy's health check sees it, or Off while CrowdSec is off (see [CrowdSec](crowdsec.md#bouncer-state)), or Unknown while HAProxy does not answer, with the active bans and captchas of your own engine and manual bans, read once a minute, and whether a community blocklist is loaded. The HomeProxy version and a newer release. When the last apply ran and whether it succeeded |
| Needs attention | Shown only when something is wrong: HAProxy does not answer, the last apply failed, a route is down, a certificate is close to expiry, a background job failed (such as a renewal or a DDNS update), or the DNS server or CrowdSec bouncer is down. Each line links to the page where you fix it. A job that failed and then succeeded is not listed |
| Requests | Requests in the last 24 hours in 15-minute steps, and the number of server errors (`5xx`) |
| Route health | One line per live route, named by its host and path: up, down or not checked, as HAProxy sees its upstreams, with the requests and `5xx` responses counted since HAProxy last reloaded. `tcp` routes show no counters |
| Responses | The share of `2xx`, `3xx`, `4xx` and `5xx` responses in the last 24 hours |
| Top countries | The five countries with the most requests in the last 24 hours. LAN clients have no country |
| Certificates | The five certificates closest to expiry, with the days left |

With no domain and no route, Overview shows the three first steps instead: add a domain, add a route, apply.

HAProxy and the dashboard are separate processes. If HAProxy does not answer, the page still loads and shows HAProxy's error, and the proxy keeps serving.

A route that is `DOWN` is live. HAProxy serves it with an error until the upstream answers, and it comes back by itself. A warning after apply says the same.

### Update check

When the dashboard starts, it asks endoflife.date for the newest patch of the HAProxy branch it runs, and compares it with the running version. If the lookup fails, for example without internet access, the next try is a day later, so a newer patch can be missing for up to a day after a restart. The check changes nothing. Updating means pulling or building a newer image, as in [Install](install.md).

HomeProxy also checks for a newer release of itself. The check runs when the dashboard starts, and then once a day. It reads the newest release on GitHub. Versions are `X.Y.Z`, and a release is tagged `vX.Y.Z`. If the check has not succeeded yet, for example without internet access, the status strip shows "Not checked yet".

## Jobs

Work that takes longer than a request runs as a job. Today that is certificate issuance and renewal, and the daily checks. The Domains page shows each certificate's job: "Issuing…" while it runs and, for a failed one, the error. Jobs are kept in memory only, so the list is empty after a restart. A failed job does not change HAProxy.

## Audit log

Every change that the dashboard makes is appended to `/data/audit/audit.jsonl`, one JSON object per line, with the time, the user, the action and its details. Secrets are never written to it. There is no page for it yet. Read the file on the host, or call `GET /api/v1/audit?limit=100`, with a logged-in session.

| Action | Written when | Details |
| --- | --- | --- |
| `apply` | An apply runs, whether it succeeded or failed | The stage reached, the path taken (`none`, `map` or `reload`), the snapshot id and the errors |
| `dashboard_listen` | A new dashboard address is confirmed | The address |
| `dashboard_settings` | The session length is saved | The session length |
| `keys` | CrowdSec, captcha or MaxMind keys are set or cleared | The names of the keys, never their values |
| `notification_urls` | Notification or heartbeat URLs are set or cleared | The names of the targets, never a URL |
| `security.decision.add` | A ban or captcha is added | The address or range, the type and the duration |
| `security.decision.delete` | A decision is deleted, for example an unban | The decision id, and its address or range, type and origin |
| `security.allowlist.add`, `security.allowlist.remove` | An allowlist entry is added or removed | The entry |
| `cloudflare.adopt` | A hand-made DDNS record is adopted | The zone and the record |
| `cloudflare.purge` | The route records HomeProxy manages in Cloudflare are removed | The records removed and the errors |
