# Dashboard access and passwords

The dashboard is for your LAN only. It always uses HTTPS, it has no default password, and it ignores clients outside your LAN ranges.

## First run

On the first start there is no `config.yaml`, so the dashboard starts in setup mode:

1. It makes a self-signed certificate at `/data/tls/dashboard.pem` and keeps it for later starts.
2. It listens on port 8443 of the address that holds the default route, for example `https://192.168.1.10:8443`. Set `HOMEPROXY_BOOTSTRAP_LISTEN` to `ip:port` to choose another address.
3. It accepts connections only from private addresses: 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, loopback, link-local and IPv6 unique local addresses.
4. It prints a one-time setup token to the container log:

   ```sh
   docker compose logs homeproxy
   ```

   The log also shows the certificate's SHA-256 fingerprint. Compare it with what your browser shows before you accept the certificate warning.

Open the address in the log, enter the token, and set a password of at least 10 characters. HomeProxy detects the LAN ranges, the proxy's LAN IP and the internal DNS server address, and asks only for a value it cannot detect. Your own address is added to the LAN ranges when it is outside them. See [Install, step 5](install.md#5-finish-setup). The token works once. If setup fails, for example on a too-short password, the same token works again. If the dashboard restarts before you finish, it prints a new token and the old one stops working.

The first domain is added after setup, on the Domains page, because adding a domain checks its DNS token.

When setup finishes, the dashboard stays on the address it is on and saves it as `dashboard.listen`. To move it, see [Change the dashboard address](#change-the-dashboard-address).

## After setup

- The dashboard listens on `dashboard.listen` and accepts connections only from `network.lan_cidrs`. A client from any other address gets no HTTP response: the connection closes before a request is read.
- A change to `network.lan_cidrs` applies to the next connection, with no restart. To change `dashboard.listen` from the dashboard, see [Change the dashboard address](#change-the-dashboard-address). A hand edit of `dashboard.listen` needs a restart of the dashboard: `docker compose exec homeproxy /command/s6-svc -r /run/service/api`, or restart the container.
- Sessions are signed cookies. They are `HttpOnly`, `Secure` and `SameSite=Strict`, and they expire after `dashboard.session_ttl` (12 hours by default, at least 5 minutes when set from the dashboard). A change from the dashboard is saved at once and applies to every open session. When a session expires, the next request the open dashboard makes sends you to the login page. Log out ends the session at once, also after a restart.
- Browsers send the session cookie to every port of the dashboard's address, not only the dashboard's port. Do not open another HTTPS service on the same address, such as a `tcp` route that keeps an app's own TLS, in the browser you use for the dashboard, unless you trust that service: it receives the cookie. Use another address or host name for it.
- Every response tells the browser not to show the dashboard inside a frame on another page.
- After 5 wrong passwords from one address in 15 minutes, that address must wait until the oldest failure is 15 minutes old. During the wait even the right password is refused with `429 Too Many Requests` and a `Retry-After` header.
- Every request that changes something (any method except GET, HEAD and OPTIONS) must carry an `Origin` header equal to the dashboard's own address, for example `Origin: https://192.168.1.10:8443`. Browsers send it on their own. Scripts that call the API must add it.

If the dashboard settings in `config.yaml` cannot be used, for example after a typo in `dashboard.listen`, the dashboard starts in recovery mode: on the setup address, private addresses only, with logins refused until the file is fixed. The log names the field. Once the file is fixed, only `network.lan_cidrs` may connect. The dashboard stays on the setup address until it restarts.

## When the saved address is gone

If the dashboard cannot listen on `dashboard.listen` when it starts, for example because the host's LAN IP changed or was not up yet at boot, it listens on the setup address instead (see [First run](#first-run)) and logs why:

```text
cannot listen on dashboard.listen 192.168.1.10:8443: Cannot assign requested address; using the setup address until a restart
```

- Only private addresses may connect, as in setup mode, because `network.lan_cidrs` may be as stale as the address, for example after a new router. The password is still required, and logins work.
- The certificate stays the same, so its fingerprint does not change. It does not name the new address, so the browser shows its usual warning for the self-signed certificate.
- The dashboard stays on the setup address until it restarts or you move it. To move it, fix `network.lan_cidrs` on the Settings page and apply if it is stale, then [change the dashboard address](#change-the-dashboard-address). The new address accepts `network.lan_cidrs` again.
- To keep the address you are on, usually port 8443 of the new LAN IP, save it as the dashboard address. Your request already reached it, so it is saved at once, with no 60-second check. The dashboard keeps accepting private addresses only until it restarts, and from then on it listens there and accepts `network.lan_cidrs`.
- After a slow boot, a restart of the dashboard goes back to `dashboard.listen`: `docker compose exec homeproxy /command/s6-svc -r /run/service/api`.
- HAProxy's address is private too, so a public route to the setup address would put the login on the internet while the dashboard is there. A route to it is refused while it is also `dashboard.listen`. When you moved the dashboard to another port, such a route is allowed, because another service may own the setup port, and apply warns about it.
- If `HOMEPROXY_BOOTSTRAP_LISTEN` names the old address too, the dashboard listens on every address on that port instead, still private addresses only, and logs that too. Correct the variable in `.env` when you can.

## Change the password

On **Settings**, **Dashboard and password**, choose **Change password** and enter the current password and the new one. Every other session is signed out, and your own session stays open. A wrong current password counts toward the login limit above.

The dashboard refuses the change while a change to `config.yaml` is pending, for example a settings, domain or DNS change. The pending copy of `config.yaml` holds the old password hash. If you apply that copy, the old password comes back. Apply or discard that change first. Other pending changes, such as a route, do not stop a password change.

## Settings

The Settings page edits the network, the internal DNS server, certificates (Let's Encrypt staging), Cloudflare (on or off, and the sync interval), CrowdSec, the GeoIP provider, notifications and the Prometheus exporter. The links at the top jump to each section. **Stage changes** at the bottom says which sections you changed and puts them into the pending changes, like a route edit; they take effect on the next apply. Only the values you change are written to `config.yaml`, so its comments and the rest of the file stay as they are. Defaults are never written: a value you set back to its default is removed from the file.

An address or range in `network.lan_cidrs` that reaches outside the private ranges (RFC 1918, loopback, link-local and IPv6 unique local), such as `0.0.0.0/1`, is saved with a warning that names it, and every apply repeats the warning. Its clients count as LAN, so LAN-only routes and the dashboard answer them. A catch-all range (`0.0.0.0/0`, `::/0`) is refused.

The page **Backup** is the last entry in the Settings menu. It is also saved at once. It downloads a backup, makes the backup token and restores a backup. See [Back up](install.md#back-up).

The dashboard address, the session length and the password are on their own page, **Dashboard and password**, the entry above **Backup** in the Settings menu. The address and session length are saved to `config.yaml` at once, without an apply. While a change to `config.yaml` is staged, the page tells you this and its Save waits. An apply of the staged copy of `config.yaml` would undo the change. A staged route does not stop the Save.

The CrowdSec, captcha and MaxMind keys are write-only and saved at once: the page shows only whether each one is set. A set key stays visible, with **Clear**, even while its feature is off. Saving a key sets or replaces it in `secrets.yaml`, and saving it empty clears it. No key value is ever shown, logged or written to the audit log. See [CrowdSec](crowdsec.md) and [GeoIP](geoip.md).

## Change the dashboard address

1. Open **Settings**, then **Dashboard and password**.
2. In **Dashboard address**, enter the new address as `ip:port`, for example `192.168.1.10:8443`.
3. Click **Save**.
4. Click **Open the new address** and log in there within 60 seconds.

In `config.yaml`, the address is `dashboard.listen`. A hand edit needs a restart of the dashboard (see [After setup](#after-setup)).

A wrong address could lock you out, so a new `dashboard.listen` is tried before it is saved:

1. The dashboard refuses an address this host cannot listen on, for example an IP that is not on the host or a port in use. It also refuses an address the next apply would refuse, for example a port HAProxy uses (even while HAProxy is stopped), or one a route forwards to, which could put the dashboard on the internet.
2. It starts listening on the new address and keeps the current one open. The answer holds the new URL.
3. Open the new URL within 60 seconds. If your browser has no session there, which happens when the IP changes, log in again. Your first page load with a session on the new address saves it.
4. If no logged-in request reaches the new address within 60 seconds, the dashboard closes it and keeps the current one. A restart of the dashboard during the 60 seconds also keeps the current one.

Because both addresses listen during the 60 seconds, a move between `0.0.0.0` and one IP on the same port, such as from `0.0.0.0:8443` to `192.168.1.10:8443`, is refused. Move to another port first, then to the address you want.

A few seconds after the change is saved, the old address stops answering, so you can move back to it later. The same certificate serves both, so its fingerprint does not change.

Restoring a snapshot from History keeps the current password, address and session length, so a restore cannot lock you out. A restore of a full backup is different: the password becomes the password of the backup. See [Restore a backup](install.md#restore-a-backup).

Moving between one IP and every address (`0.0.0.0`) on the same port is refused, because both cannot listen at once during the 60 seconds. Move to another port first, or edit `config.yaml` and restart the dashboard.

## Reset the password

Run this on the Docker host, in the directory of `compose.yaml`:

```sh
docker compose exec homeproxy /opt/homeproxy/venv/bin/homeproxy set-password
```

It asks for the new password twice. To read it from a file or pipe instead, add `--stdin` and `-T` to `docker compose exec`; the first line is the password.

The command changes only `dashboard.password_hash` in `config.yaml`, and keeps comments. The running dashboard reads the new hash on its next request, so no restart is needed. Every open session is signed out.

A `config.yaml` change that waits to be applied gets the new hash too and keeps its edits, so applying it keeps the new password. If `config.yaml` was also edited by hand after that change, the dashboard still reports the conflict.

`docker compose exec` runs as root, so the command switches to the `homeproxy` user before it touches `/data`. Files it writes stay owned by that user. It refuses to run as any other user.

## Files

| File | Holds |
| --- | --- |
| `/data/tls/dashboard.pem` | The dashboard's private key and self-signed certificate, mode 0600. Delete it to make a new one on the next start |
| `/data/config/config.yaml` | `dashboard.password_hash` (argon2id), `dashboard.listen`, `dashboard.session_ttl` |
| `/data/secrets/secrets.yaml` | `session_key`, made on the first start. Changing it signs out every session |
| `/data/state/revoked-sessions.json` | Sessions ended by logout, until their cookies would expire |
