# Notifications

HomeProxy can send a message to Discord or to any webhook receiver when something fails, so you do not have to watch the dashboard.

## Targets

```yaml
# config.yaml
notifications:
  targets:
    - name: discord-main
      type: discord            # or webhook
      events: [certificate_failed, apply_failed]   # leave out to get every event
```

```yaml
# secrets.yaml
notifications:
  targets:
    discord-main: {url: "https://discord.com/api/webhooks/..."}
```

- **`discord`** posts to a Discord webhook URL: a one-line `content` and one embed with the title, the details, a color for the severity and the time.
- **`webhook`** posts JSON to any URL. Slack, Mattermost and most self-hosted receivers show the `text` field. The other fields are for receivers that read them:

  ```json
  {
    "text": "HomeProxy: Apply failed at validate\napp.example.org is in no configured domain",
    "event": "apply_failed",
    "severity": "error",
    "title": "Apply failed at validate",
    "message": "app.example.org is in no configured domain",
    "time": "2026-11-11T21:06:00+00:00"
  }
  ```

The targets are part of the settings: change them on the Settings page or with `PUT /api/v1/settings` (the `notifications` section). Like every setting, a change is pending until you apply it.

## URLs are secrets

Anyone with a webhook URL can post to your channel, so a URL is written once and never shown again.

- `PUT /api/v1/notifications/urls` with `{"targets": {"discord-main": "https://..."}}` saves a URL at once, with no apply. Send `""` to clear one. A URL you leave out stays as it is.
- `GET /api/v1/notifications/urls` says only which targets have a URL. It also lists under `unused` the names that have a URL but no target, for example after you rename or remove a target, so you can clear them. A renamed or removed target counts as unused only after the apply, because until then the live settings still send to it, and a discarded change brings it back.
- Logs and the audit log name the target, never its URL. A URL is redacted wherever it would appear.
- A URL must start with `http://` or `https://` and must not hold a user name or password.

`heartbeat_url` in the same section of `secrets.yaml` is the heartbeat's ping URL. Send `{"heartbeat_url": "https://..."}` to the same endpoint to set it. See [Heartbeat](#heartbeat).

## Test a target

`POST /api/v1/notifications/targets/<name>/test` sends one sample message and returns the receiver's answer: `{"status": 204, "error": null}`, or `{"status": null, "error": "..."}` when no answer arrived. It uses the targets with your pending changes, so you can test a target before you apply it.

## Heartbeat

HomeProxy cannot send a message when the host, the container or HAProxy is down. The heartbeat covers that: HomeProxy pings a URL at a fixed interval, and an outside service such as [healthchecks.io](https://healthchecks.io) or an Uptime Kuma push monitor alerts you when the pings stop.

```yaml
# config.yaml
notifications:
  heartbeat:
    enabled: true
    interval: 1m        # at least 30s; s, m, h or d
```

```yaml
# secrets.yaml
notifications:
  heartbeat_url: "https://hc-ping.com/<uuid>"
```

- HomeProxy sends a `GET` to the URL every interval, within 5 seconds, but only while HAProxy answers on its socket. While HAProxy does not answer, no ping is sent, so the outside service alerts. When HAProxy answers again, a ping is sent at once.
- Each ping waits at most 5 seconds and is not retried: the next interval is the retry. Only a 2xx answer counts. A redirect is not followed.
- A ping that fails is logged once, and once more when pings work again. The URL is never logged or returned.
- Set the outside service's period to the interval, with a grace time of at least one more interval.
- The heartbeat settings are part of the settings (`PUT /api/v1/settings`, the `notifications` section) and take effect after an apply. The URL is saved at once, like the target URLs. Set the URL first: turning the heartbeat on while no URL is set is refused with "Set the heartbeat URL before you turn the heartbeat on."
- `POST /api/v1/notifications/heartbeat/test` sends one ping, whether the heartbeat is on or not, and returns `{"status": 200, "error": null}`, or `{"status": null, "error": "..."}` when no answer arrived. It answers 409 when no URL is set.

## Events

| Event | Sent when | Severity |
| --- | --- | --- |
| `certificate_failed` | A certificate issue or renewal job fails. The daily renewal check sends one message that names each certificate that failed, so a certificate that keeps failing is reported once a day | error |
| `apply_failed` | An apply fails or rolls back. The title says at which stage | error |
| `ddns_failed` | The DDNS update starts failing. One message is sent, and the next one only after DDNS has worked again. A restart does not repeat it. Until a target accepts the message, each failed update sends it again, so an outage that also stops the webhook does not swallow it | error |
| `geoip_failed` | A GeoIP data refresh fails. A refresh you start sends it each time. The hourly retries send one message per run of failures, and send it again at the next failure until a target accepts it | error |
| `new_version` | A new HomeProxy release is out. One message per version, also across restarts. The check sends it again each day until at least one target accepts it, so a target you add later still hears of the current version | info |
| `certificate_expiring` | A certificate expires within 7 days. One message per certificate until it renews, also across restarts | warning |
| `route_down` | A route has had no healthy server for 2 minutes, and again once it recovers. Every enabled route with upstreams is watched. A route without a health check path is checked by a TCP connect | error, info on recovery |
| `bouncer_down` | HAProxy's check of the CrowdSec bouncer has failed for 2 minutes, and again once it recovers. While it is down, requests pass without CrowdSec checks | error, info on recovery |
| `appsec_down` | AppSec has not allowed the dashboard's check request for 2 minutes, and again once it recovers. While it is down, requests pass without AppSec checks | error, info on recovery |

Route and bouncer health come from HAProxy's own checks, read every 15 seconds. AppSec health comes from a check request that the dashboard sends every 15 seconds. A short outage, such as a restart of the upstream, sends nothing. Each change of state sends one message, and `/data/state/alerts.json` records what was sent, so a restart of HomeProxy does not send it again. A route that is disabled or removed while it is down sends no recovery message. To get no route messages, leave `route_down` out of a target's events.

## Delivery

- Messages are sent in the background. A slow or dead receiver never blocks or fails an apply or a job.
- Each send waits at most 5 seconds and is retried 3 times, after 1, 2 and 4 seconds. A send that still fails is logged with the target name. A target that fails never stops the others.
- Only a 2xx answer counts as sent. A redirect is not followed: a webhook that moved counts as a failed send, so update its URL.
- Discord messages never ping anyone, even when an error text holds `@everyone` or a mention.
- Every message is redacted: no value from `secrets.yaml` ever reaches a receiver.
- Messages go to the targets in the applied `config.yaml`, never to pending ones.
