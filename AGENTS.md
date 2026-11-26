# HomeProxy development instructions

HomeProxy is a self-hosted reverse proxy manager for home labs. It is one container with HAProxy and a small dashboard. HAProxy does all the proxying. The dashboard writes the config and talks to the HAProxy sockets. One admin uses it, from the LAN only.

The code is the source of truth. This file gives the overview and the rules that the code cannot show.

## How it works

The container runs these s6 services. The CrowdSec engine is optional and runs in a separate container.

| Service | What it does |
| --- | --- |
| `haproxy` | Serves all traffic. It starts with `-f /data/generated/conf.d/`. Master CLI `/run/haproxy/master.sock`, runtime API `/run/haproxy/admin.sock` |
| `api` | The FastAPI app and the built UI over HTTPS on `dashboard.listen`. It also runs the jobs and the syslog collector (`/run/homeproxy/syslog.sock`) |
| `dns` | The internal DNS server. It reads `dns-records.json` from the live release and reloads on SIGHUP |
| `spoa` | The CrowdSec bouncer. It stays down until CrowdSec is on |
| `*-log` | s6-log for the api, dns and spoa services |

The api and dns services run as the `homeproxy` user. Only HAProxy binds privileged ports, through a file capability. The container uses host networking.

Data in `/data`:

| Path | Contents |
| --- | --- |
| `config/` | `config.yaml` and `routes/*.yaml`. The user may edit these files by hand |
| `pending/` | Staged edits and the hash of each staged file's base |
| `secrets/secrets.yaml` | All secrets, mode 0600. Never in snapshots |
| `history/` | Snapshots for rollback, the last 50 |
| `releases/<id>/`, `generated` | Rendered HAProxy files. `generated` is a symlink to the live release |
| `certs/`, `acme/`, `tls/` | Certificates, the lego account and the dashboard's self-signed certificate |
| `state/` | Metrics in SQLite, stored logs and caches. It is disposable |
| `audit/audit.jsonl` | The append-only audit log |

An edit from the API goes into `pending/` only. An apply does these steps:

1. Validate the overlay of `pending/` on `config/`.
2. Render a new release and run `haproxy -c` on it.
3. Update HAProxy. Maps and certificates change through the runtime API. Other changes need a reload.
4. Verify HAProxy. If the verification fails, roll back to the previous release.
5. Promote the edits to `config/` and take a snapshot.

A failed apply changes nothing in `config/`, and the edits stay pending. If a staged file changed by hand after it was staged, the apply stops with a conflict.

## Where to find things

| Question | Look in |
| --- | --- |
| Config file format | The Pydantic models in `backend/homeproxy/models/` and `examples/data/` |
| API | The routers in `backend/homeproxy/api/` and the OpenAPI schema |
| How a feature behaves for the user | `docs/` and the tests |
| How to write and test code | [docs/code-style.md](docs/code-style.md) |
| Planned work | GitHub issues and the milestones |
| What is not planned, and why | The pinned "Not planned" issue |

Backend packages in `backend/homeproxy/`: `models` (schemas), `store` (YAML, pending edits, snapshots, locks, audit), `generator` (templates and render), `haproxy` (sockets, stats, apply pipeline), `certs` (lego), `cloudflare` (records, DDNS), `dns`, `logs` (collector, metrics, stored logs), `crowdsec`, `geoip`, `auth`, `api`, `jobs`, `notify`, `util`. The frontend is a Nuxt 4 app in `frontend/app/`. The image is in `image/`, the compose files are in `deploy/`, and the tasks are in `mise-tasks/`.

## Design rules

These rules are settled. Do not change them without a new decision from the maintainer.

- The dashboard is never an upstream of a route, and it never answers from the internet. A non-LAN client of the dashboard port gets no HTTP response.
- The dashboard uses HTTPS with a self-signed certificate. There is no default password. Setup needs a one-time token from the container log and a private source address.
- An unhealthy upstream after an apply gives a warning. It does not cause a rollback.
- A snapshot restore never restores the password hash.
- CrowdSec fails open. HAProxy skips the check for LAN ranges and for its own allowlist (`allow.lst`).
- HomeProxy changes or deletes only the Cloudflare records that have its ownership marker. A hand-made record changes only after the admin adopts it.
- Certificates use DNS-01 through lego, with any lego DNS provider except `exec`. Cloudflare records and DDNS need a Cloudflare zone.
- The UI can edit every setting in `config.yaml` and `secrets.yaml`, and a new setting ships with its UI. The exceptions are `version`, `session_key`, `haproxy.track_branch` and `network.ipv6`.
- The API never returns a secret. It returns only whether a secret is set. The two exceptions are the backup download and the backup token, which is shown once when it is made.
- GeoIP blocks countries. HAProxy holds only the ranges of the countries that are blocked somewhere.
- The UI has a dark theme only, and it makes no request outside its origin.
- HomeProxy listens and answers DNS on IPv4 only. It tracks the newest HAProxy LTS.

## Commands

Development tools are pinned in `mise.toml`. Run `mise install` once.

| Command | Purpose |
| --- | --- |
| `mise run check` | Lint, types, unit and golden tests. Run before every push |
| `mise run test-integration` | Build the image, smoke-test it, then run the full container tests in compose |
| `mise run test-e2e` | Build the image, then Playwright: real flows through the image's dashboard (`tests/real`) and mocked-API specs for error states (`tests/e2e`) |
| `mise run image` | Build the image for this machine's architecture |
| `mise run dev` | Start the dev stack with its data volume as it is (empty on first start). `-- seed` loads the example data, `-- reset` deletes the data |
| `mise run api-client` | Regenerate the TypeScript client and JSON schema |
| `mise run golden-update` | Update golden files. Review the diff |
| `mise run bump <kind>` | Bump the version (`major`, `minor` or `patch`) on `dev` and write the release PR description. The `release-pr` workflow runs it. Run it by hand only for a breaking change |
| `mise run release-kind` | Print what the commits since the last release call for: `none`, `patch`, `minor` or `breaking` |
| `mise run release-notes` | Print the release notes for the commits since the last release. `--closes` adds the `Closes #N` lines for the release PR |

Each command is one executable file in `mise-tasks/`.

## Code rules

1. Never build a shell command from user input. Run `haproxy`, `lego` and `git` with fixed argument lists through `homeproxy.util.run`.
2. User values reach generated HAProxy files only through strict validators and the escaping filter.
3. Never log or return secrets. Anything from `secrets.yaml` is redacted in logs, API responses, errors and diffs, except in the backup download (see Design rules). Generated files that hold hashes never appear in the UI.
4. Never hard-code a user's domain, IP or port. Examples use `example.com` and `192.168.1.10`. Fixed values of the product, such as a default port or a public service URL, are named constants.
5. Keep HAProxy independent: no code path may stop HAProxy because the dashboard failed.
6. All file writes are atomic: temp file in the same directory, `fsync`, rename.
7. Python: 3.13, type hints everywhere, `ruff` and `mypy --strict`. TypeScript: strict mode, ESLint, components in `<script setup>`.
8. Tests must not bind fixed host ports, so they can run beside the dev stack.
9. Add a dependency only when the change needs it, and pin it where Renovate can update it (`renovate.json`): an image by tag and digest, a download by version with a `# vX` comment on each checksum line, a tool in `mise.toml`.

## Code style and tests

Read [docs/code-style.md](docs/code-style.md) before you write, change or review code or tests. It has the rules for all code, the backend, the frontend and the tests, and it is binding for every change.

## Evidence

1. **Back every claim with evidence.** "Broken", "fixed", "passes" and "works" each need command output, a test result, a reproduction or a `file:line`. Without evidence, say it is unverified.
2. **Trust the diff, not the summary.** Review a code change by reading its diff. A report of what changed only points where to look.

## Writing

Write all text for people in plain English, with no filler. Write `docs/` and step-by-step text in ASD-STE100 Simplified Technical English. In `docs/`, tell how to do a task in the dashboard first, and give the `config.yaml` key after it for hand edits. Quote UI labels exactly. A change to a UI label also changes its tests and the docs that quote it.

## Workflow

- `main` holds the latest release and is the default branch. All work goes to `dev`.
- Commit straight to `dev` by default. There is one maintainer, so a branch only adds steps.
- `dev` must always be releasable: a `fix` on `dev` ships by itself the next day. Work that is not ready to ship stays on a branch.
- For a small issue, put `Closes #N` in the commit message. The release PR description repeats it, and that closes the issue when the release merges.
- Use a branch and a PR into `dev` (`gh pr create --base dev`) only for issue work that needs one, such as work over several sessions, work that needs a review, or a feature split over several issues. A feature stays on `feat/<issue>` until its backend and its UI are both done. Put `Closes #N` in the PR description. The squash commit keeps it, so the release PR description finds it.
- Before a push, run `mise run check`. Also run `test-integration` when runtime behavior or the image changes, and `test-e2e` when the UI changes. User-visible behavior goes in `docs/`.
- Commit messages and PR titles use Conventional Commits, for example `feat(generator): add LAN-only map`. The type decides the next release: `fix` makes a patch release that publishes by itself, `feat` makes a minor release that the maintainer merges, `!` or `BREAKING CHANGE` waits for a release by hand, and every other type releases nothing. Use `fix` only for a change a user would want shipped now.
- Renovate owns the dependency pins and opens its PRs into `dev`. Do not bump a pin by hand unless the change needs a newer version. Renovate reads `renovate.json` from `main`, so an edit to it takes effect after the next release.
- Sign off every commit with `git commit -s`.
- Never commit secrets, generated client output or local data.
- If your session gives you commit attribution lines, end each commit message with them.

Releases are automatic; [docs/releases.md](docs/releases.md) has the whole cycle. Once a day the `release-pr` workflow reads the commits on `dev` since the last release (`mise run release-kind`), bumps the version and opens or refreshes the release PR from `dev` into `main`, with the `release` label and a `Closes #N` line for each issue the commits name. A patch release merges by itself once every required check passes. A minor release waits for the maintainer's merge, and a breaking change waits for `mise run bump` by hand. The maintainer can start the workflow from the Actions tab, for example to ship a Debian security fix that arrived as a `chore`. Release PRs merge with a merge commit, never a squash. The merge builds and tests each architecture, publishes the tested image with its tags and attestation, and creates the GitHub release. Commit subjects become release notes, so write them for users.

## The AGENTS directory

`AGENTS/` at the repository root is for working notes and is not in git. Use it for a plan or a handover that is too large for an issue. Use one file for each piece of work, named `<issue number>-<slug>.md` or `<slug>.md`, with these sections:

- Goal: the issue link and the result you want.
- Plan: the steps, as a checklist.
- Findings: facts you found, with evidence.
- Handover: what is done, the next step and the open questions.

Before the work closes, move the decisions to the issue or the PR, and move the behavior to the code, the tests or `docs/`. Then delete the file. A small task from an issue needs no file.
