# Metrics and logs

## Metrics

The Metrics page shows the requests HAProxy logged. Pick a range at the top, from the last hour to the last 90 days.

- **Three numbers:** the requests in the range, the client errors (4xx) and the server errors (5xx), each with its share of the requests.
- **The chart** draws requests over time. Each point counts the requests of one bucket, named above the chart: a minute for the last hour, 5 minutes for 6 hours, 15 minutes for 24 hours, an hour for 7 days, 6 hours for 30 days and a day for 90 days. The last point is the bucket still running, so it holds fewer requests; its tooltip says "so far".
- **Sites, Status codes and Countries** rank the requests in the range, each with a bar for its share. Click a row to see it over time: the chart draws it in orange, with all requests as a thin grey line behind it. Click it again, or **Show all**, to go back.

The country comes from the GeoIP data (see [GeoIP](geoip.md)) for every request, whether its country is blocked or not. Lines with no value get their own row: "No site" for a request that matched no site, "No status (TCP)" for a TCP connection, and "Unknown" for an address with no country. A client in `network.lan_cidrs` has its own row, "LAN". Before version 0.3.0, HomeProxy counted LAN requests in "Unknown", and they stay there. A site you removed keeps its internal name, marked "(removed)".

The data is stored in `state/metrics.db` in the data directory, so charts survive a container restart. If the file is damaged, HomeProxy replaces it and the history starts again. If it cannot be opened at all, the page shows "Metrics are unavailable". Routing is not affected.

## Logs

The Logs page has a tab for each log HomeProxy stores: Requests, HAProxy, Dashboard, DNS and Bouncer.

**Requests** tails the access log live, newest line first, up to 500 lines. Each line shows the time, the status, the request, the site it went to ("No site" when none matched), the client address and its country. The light says "Live", "Reconnecting" while the dashboard connection is down, or "Stopped" when a filter is refused.

- **Filters:** a site, the client's network (**All**, **LAN** or **Internet**), a status code, a client address or range (for example `192.168.1.0/24`) and a country code. **LAN** shows the clients in `network.lan_cidrs`, and **Internet** shows all other clients. The country column shows "LAN" for a LAN client. The site and the network apply at once; the others when you press Enter or leave the field. Only lines that match every filled field show. **Clear** removes them. A filter the dashboard refuses says why under the filters.
- Click a line to see it as HAProxy wrote it, with the full date and request.
- **Pause** stops new lines from entering the list, so you can read it. The light counts the lines that arrived meanwhile, and **Resume** puts them on top. Changing a filter ends the pause.
- **Download** takes the whole stored access log.

When the connection reopens, the page clears the list and the dashboard sends its kept lines again.

The other tabs show the last 200 lines of that log, newest at the bottom, with its size, when it last changed, **Reload** and **Download** for the whole log.

## Stored logs

HomeProxy also stores its logs in `state/logs/<source>/` in the data directory, one tab each on the Logs page: Requests (`access`), HAProxy (`haproxy`), Dashboard (`api`), DNS (`dns`) and Bouncer (`spoa`). Each source downloads as one text file, its oldest line first, through `GET /api/v1/logs/files/{source}`. `GET /api/v1/logs/files` lists the sources with their size.

| Source | Holds | Kept |
| --- | --- | --- |
| `access` | Every access line HAProxy logged, as HAProxy wrote it, after its time | `access.log` and 4 rotated files of 10 MiB, at most 50 MiB |
| `haproxy` | HAProxy's other messages, such as a server going down or a proxy starting | `haproxy.log` and 4 rotated files of 1 MiB, at most 5 MiB |
| `api` | The dashboard's output | `current` and 4 archives of about 1 MB, at most about 5 MB |
| `dns` | The internal DNS server's output | the same as `api` |
| `spoa` | The CrowdSec bouncer's output | the same as `api` |

All of them together stay under about 70 MB, so a small disk such as a Raspberry Pi's SD card can hold them. When a file is full, the oldest one is deleted. The `state` directory is not backed up (see [Install](install.md)), and these logs are not needed to restore HomeProxy.

`docker logs` shows the same service output as before. The stored `api` log leaves out two kinds of line, which `docker logs` still shows: the setup token, and the requests to `/api/v1/healthz` that a container health check sends every few seconds.

What is not stored:

- The CrowdSec engine's logs. It runs in its own container: use `docker logs` on it.
- HAProxy's startup messages, such as configuration warnings. HAProxy prints them before its logging starts, so they reach only `docker logs`. When HAProxy refuses an apply, the apply result holds its startup log.
- Access lines and HAProxy messages that arrive while the dashboard is not running. The dashboard writes these two files, and HAProxy never waits for it.

A full disk does not stop a service or HAProxy. Lines that cannot be written are lost, and the dashboard logs when its writes fail and when they work again.

The access log holds client IP addresses and full URLs with their query strings, as clients sent them. Some apps put tokens in a query string. The live tail shows the same lines. Secrets that HomeProxy knows, such as its keys, are replaced by `[REDACTED]` in the dashboard's own output, in the access log and in the live tail. So is the path of a notification URL, when the receiver is a route on this proxy, if the path and query are at least 16 characters long. A stored log downloads only from the dashboard, after login.

## HAProxy updates

HAProxy is bundled in the HomeProxy image. A newer HAProxy comes only with a new HomeProxy release. The HomeProxy update check tells you when a new release is available, as in [Overview and status](overview-and-status.md#update-check). To get it, update the image as in [Install](install.md#update).
