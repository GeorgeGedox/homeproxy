# Basic auth for routes

An `http` route with auth `basic` answers `401 Unauthorized` with a `WWW-Authenticate: Basic` header until the browser sends a known user name and password. Use it for a service that has no login of its own. It does not replace one for anything you expose to the internet.

Once the check passes, HAProxy removes the `Authorization` header, so the upstream never sees the user name or the password. This cannot be turned off. An upstream that needs its own `Authorization` header can get one from the route's `request_headers`.

## Set users

In the route editor, turn on **Ask for a password** under **Protection** and press **Add user** for each user. A route needs at least one user: the editor refuses to stage it without one, and apply fails for such a route.

- A new user needs a password of 8 to 1024 characters, without control characters.
- A user that already exists shows the password field as "Unchanged". Leave it empty to keep the password, or type a new one to replace it.
- Remove a user with the x button.

Users are saved at once, not as a pending change, and take effect at the next apply. After you change the users of an existing route, the bar at the bottom shows the change with Apply. See [Pending changes, apply and history](changes-and-apply.md) for what happens to the users when a route is renamed or deleted.

## Where passwords are kept

HomeProxy hashes each password with SHA-512 crypt (5000 rounds) and keeps only the hash, in `/data/secrets/secrets.yaml` under `basic_auth.<route name>`. The password is write-only: the dashboard and the API return user names, never passwords or hashes.

Hashes appear in no snapshot, diff, log or API response. The generated HAProxy file that holds them (`15-userlists.cfg`) is readable only by the HAProxy user and is left out of the diffs.

HAProxy checks the hash on every request, so a very high round count costs CPU. A hash that you write into `secrets.yaml` by hand must be a SHA-512 crypt hash (`$6$...`) with at most 50,000 rounds. Anything else is refused.

## Check it

```sh
curl -ik https://app.example.com/                 # 401
curl -ik -u alice https://app.example.com/    # asks for the password, then 200
```
