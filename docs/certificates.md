# Certificates

HomeProxy gets its certificates from Let's Encrypt with a DNS challenge through the domain's DNS provider. HAProxy never has to be reachable from the internet for this, and a wildcard certificate covers every host under a zone.

## In the dashboard

Each domain has a certificate setting. To set it:

1. Open **Domains**.
2. Click **Add domain**, or open a domain to edit it.
3. In **Domain**, enter the zone, for example `example.com`.
4. Select how the domain gets HTTPS:
   - **Let's Encrypt via Cloudflare**: a trusted certificate. Cloudflare DNS records and DDNS also become available.
   - **Let's Encrypt via another DNS provider**: a trusted certificate. Select the provider in **DNS provider**.
   - **Self-signed**: HTTPS that browsers warn about. It needs no account.
   - **No certificate**: plain HTTP only.
5. For a wildcard certificate, turn on **One wildcard certificate for \*.example.com**. If it is off, each host gets its own certificate.
6. Under the credentials of the provider, select stored credentials, or select **New credentials** and enter them.
7. Click **Stage change**.
8. Click **Apply** in the bar at the bottom.

HomeProxy then gets the certificates. The job list shows each issuance and its result.

In `config.yaml`, these settings are the domain keys `dns_provider`, `credentials` and `wildcard_cert`. The credentials are in `secrets.yaml`. The sections below give the details.

## What gets a certificate

| Config | Certificate |
| --- | --- |
| A domain with `wildcard_cert: true` | One certificate for `example.com` and `*.example.com` |
| An `http` route host outside that wildcard, such as `a.b.example.com`, or any host when `wildcard_cert` is false | Its own certificate for that host |
| `tcp` and `passthrough` routes | None. The backend keeps its own certificate |
| A domain with no `dns_provider` and no `credentials` | None. Its `http` and `redirect` routes are plain HTTP only, see [Route types](route-types.md#domains-without-a-certificate) |
| A domain with `dns_provider: self-signed` | The same names as above, from a certificate HomeProxy makes, see [Self-signed certificates](#self-signed-certificates) |

A host belongs to the domain with the longest matching zone. Until a certificate exists, HTTPS answers with a self-signed default certificate, so HAProxy always starts.

## DNS providers

Each domain names its DNS provider in `dns_provider`, with its credentials in `credentials`. Give both or neither, or `dns_provider: self-signed` alone. A domain with neither gets no certificate, and lego never runs for it. `wildcard_cert` is then false, and `true` is refused. Any DNS provider of the bundled lego works, except two: `exec`, which runs any program, and `manual`, which waits for a person. `GET /api/v1/domains/providers` lists the providers with the variables each one takes.

Cloudflare takes one API token, described below. Every other provider takes the environment variables that lego documents for it, for example:

```yaml
# secrets.yaml
dns_credentials:
  r53: {provider: route53, env: {AWS_ACCESS_KEY_ID: "...", AWS_SECRET_ACCESS_KEY: "..."}}
```

Any other variable name is refused, also a lower-case one, so a credential cannot set a variable such as `LD_PRELOAD` or `PATH` for lego. lego starts with only these variables, a fixed `PATH` and a private `HOME`, and with nothing else from HomeProxy's environment. HomeProxy cannot check them when you save the domain. The first issuance checks them, and a failure shows in the job list with lego's message.

Some variables that lego documents are held back:

- Profile and config-file variables are not offered: `AWS_PROFILE`, `AWS_SDK_LOAD_CONFIG`, `OCI_CONFIG_FILE` and `OCI_PROFILE`. A profile can make lego run a program.
- A variable that holds a file path, such as `AWS_SHARED_CREDENTIALS_FILE` or `GCE_SERVICE_ACCOUNT_FILE`, must name a file inside `/data/secrets`. A path that leads out of it, also through `..` or a symbolic link, is refused, and so is `secrets.yaml` itself: lego writes to some of these files, such as `ACME_DNS_STORAGE_PATH`. Where the provider also takes the content itself, such as `GCE_SERVICE_ACCOUNT`, use that instead.

Values whose variable name holds a word such as `KEY`, `SECRET`, `TOKEN` or `PASSWORD`, and that are at least 8 characters and not only digits, are hidden in logs and error messages. Settings such as a region, a TTL or an interval are not hidden, so that their digits do not garble other text.

Domains that share a credentials name must use the same provider. To move a domain to another provider, give it a new credentials name.

## Managing credentials

The DNS credentials card on the Domains page lists the stored credentials: name, provider, each variable (a secret only as "set") and the domains that use them. Replace sets new values, and is saved at once, not staged. Delete appears only on credentials that no domain uses. The API below does the same.

`GET /api/v1/credentials` lists the stored credentials: each name, its provider, its variables and the zones of the domains that use it, applied or pending. A secret value comes back as `null`: the Cloudflare token always, and every value whose variable name holds a word such as `KEY`, `SECRET`, `TOKEN` or `PASSWORD`, however short. Other values, such as a region, are shown.

`PUT /api/v1/credentials/<name>` sets the values stored under a name, with the same checks as a domain save. The provider stays. Send each variable to keep: with a new value, or with `null` to keep the stored one. A variable left out is removed. For Cloudflare, leave out `api_token` to keep the stored token. A new token must read every zone that uses the name.

`DELETE /api/v1/credentials/<name>` removes credentials that no domain uses, applied or pending. While a domain uses them, the answer names its zone. Credentials for a new name come with the domain that uses them. A snapshot whose domains use removed credentials cannot be restored.

Cloudflare DNS records, DDNS and the route switch work only on domains with `dns_provider: cloudflare`. The domain list says why they are off for any other domain (`cloudflare_off_reason`), and HomeProxy never calls Cloudflare for that zone.
## Self-signed certificates

`dns_provider: self-signed` serves a domain over HTTPS, with the redirect from HTTP, without a DNS provider or Let's Encrypt. HomeProxy makes the certificate itself when you apply, offline. It takes no `credentials`, and the staging switch does not change it. `wildcard_cert` works as for any other domain.

```yaml
# config.yaml
domains:
  - zone: example.com
    dns_provider: self-signed
```

Browsers do not trust these certificates. Each browser warns on the first visit to each name, and again for each new certificate. Apps and scripts refuse the certificate unless you turn off their check. Use it for a zone whose DNS no provider of the list manages, such as a `.lan` name that exists only on your LAN, when you still want HTTPS. If your DNS provider is in the list, a Let's Encrypt certificate is better.

A certificate is valid for 825 days, the longest that Apple devices accept. The daily check makes a new one at 30 days left, so about every 27 months, or sooner when its names change, as when you turn `wildcard_cert` on or off. Each new certificate needs the browser exception again. A domain that switches from a DNS provider to self-signed gets a self-signed certificate at the next apply. Public routes send HSTS, and a browser that saw HSTS for a name with a trusted certificate refuses a self-signed one for that name for up to a year, with no way to click through.

Cloudflare DNS records and DDNS are off for a self-signed domain.

## The Cloudflare token

Create an API token in Cloudflare for each zone, or one for several zones, with these permissions on that zone:

| Permission | Why |
| --- | --- |
| Zone, Zone, Read | Find the zone and its id |
| Zone, DNS, Edit | Write and remove the `_acme-challenge` TXT record |

When you save a domain, HomeProxy checks that the token can read the zone and its DNS records, and refuses the token with a message naming the missing permission. DNS Edit cannot be checked without writing a record, so a token without it passes the save and fails at the first issuance, with the error in the job list.

Domains that share a credentials name share one token, or one set of variables. They must all use the same provider. A new token for that name must read every zone that uses it. The token is stored in `/data/secrets/secrets.yaml` and is never shown. To replace it for every domain that uses the name, see [Managing credentials](#managing-credentials).

## Issuance

Applying a config that needs a certificate that does not exist yet starts a background job, one per certificate. Each domain on the Domains page lists the certificates it needs: "Issuing…" while a job runs, then the issuer and the days left, or "Failed:" with lego's error message and a Retry button. A certificate that no job has issued yet has an Issue now button. The job list is kept in memory, so after a restart only the installed certificates show.

When a job finishes, the certificate file is written to `/data/certs` and HAProxy reloads to load it. Reloads keep existing connections.

After an apply, a certificate that no domain or host needs any more is removed and HAProxy reloads.

## Let's Encrypt staging

`certificates.staging` in `config.yaml`, also sent with `PUT /api/v1/settings`, makes HomeProxy get its certificates from Let's Encrypt staging instead of production. Use it to test a new DNS provider or new credentials: a failed try counts against the staging rate limits, which are much higher, and not against production's. Browsers do not trust staging certificates, so turn it off again once issuance works.

```yaml
# config.yaml
certificates:
  staging: true   # default false: Let's Encrypt production
```

The switch is a staged setting like the others. After the apply, every certificate is issued again from the CA it picks, as one job per certificate. Each CA keeps its own lego state, in `/data/acme/` for production and `/data/acme/staging/` for staging, so switching back to production installs the production certificates again while they are valid, without a new issuance. The Domains page shows each certificate's issuer, and a staging issuer has `(STAGING)` in its name.

HomeProxy sends Let's Encrypt no contact email. Let's Encrypt stopped expiry emails in 2025, and HomeProxy sends its own `certificate_failed` and `certificate_expiring` notifications.

## Renewal

A check runs once a day, starting about 5 minutes after the container starts, with up to an hour of random delay. It renews each certificate with 30 days or less left, and leaves the others as they are. A renewed certificate replaces the old one in HAProxy without a reload and without dropping a connection. If one certificate fails, the others are still renewed, and the error names the one that failed.

The Renew button next to a certificate on the Domains page renews it now, even if it is not due. It is a job like any other.

## Expiry states

| State | Days left | Shown as |
| --- | --- | --- |
| `ok` | more than 21 | green |
| `warning` | 21 or fewer | amber |
| `alert` | 7 or fewer | red |

The Domains page shows each certificate's issuer, names and days left, with the exact date on hover. Overview lists the same days left.

## When issuance fails

The job's error is lego's own message. If the cause is the token, the error says `cannot read the zone example.com` and names the two permissions. Make a new token with both permissions, save it on the Domains page (Edit, then Replace the stored values), and press Retry next to the certificate.

Other causes to check:

- The token has Zone Read but not DNS Edit. The save passes, and the first issuance fails when lego cannot write the challenge record.
- The container cannot reach `1.1.1.1` on port 53 or the Cloudflare API. HomeProxy checks the challenge record against that public resolver, so a split-horizon DNS server on your LAN is not involved.
- Let's Encrypt refused the request, for example for a rate limit. Wait, then renew again.

A failed certificate is also tried again by the daily check.
