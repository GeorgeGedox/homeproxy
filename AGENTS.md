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
- The API never returns a secret. It returns only whether a secret is set.
- GeoIP blocks countries. HAProxy holds only the ranges of the countries that are blocked somewhere.
- The UI has a dark theme only, and it makes no request outside its origin.
- HomeProxy listens and answers DNS on IPv4 only. It tracks the newest HAProxy LTS.

## Commands

Development tools are pinned in `mise.toml`. Run `mise install` once.

| Command | Purpose |
| --- | --- |
| `mise run check` | Lint, types, unit and golden tests. Run before every push |
| `mise run test-integration` | Full container tests in compose |
| `mise run test-e2e` | Build the image, then Playwright: real flows through the image's dashboard (`tests/real`) and mocked-API specs for error states (`tests/e2e`) |
| `mise run image` | Build the amd64 image and smoke-test it |
| `mise run dev` | Start the dev stack with its data volume as it is (empty on first start). `-- seed` loads the example data, `-- reset` deletes the data |
| `mise run api-client` | Regenerate the TypeScript client and JSON schema |
| `mise run golden-update` | Update golden files. Review the diff |
| `mise run bump <kind>` | Bump the version (`major`, `minor` or `patch`) on `dev`, write the release PR description and print the release steps |
| `mise run release-notes` | Print the release notes for the commits since the last release. `--closes` adds the `Closes #N` lines for the release PR |

Each command is one executable file in `mise-tasks/`.

## Code rules

1. Never build a shell command from user input. Run `haproxy`, `lego` and `git` with fixed argument lists through `homeproxy.util.run`.
2. User values reach generated HAProxy files only through strict validators and the escaping filter.
3. Never log or return secrets. Anything from `secrets.yaml` is redacted in logs, API responses, errors and diffs. Generated files that hold hashes never appear in the UI.
4. Never hard-code a domain, IP or port. Examples use `example.com` and `192.168.1.10`.
5. Keep HAProxy independent: no code path may stop HAProxy because the dashboard failed.
6. All file writes are atomic: temp file in the same directory, `fsync`, rename.
7. Python: 3.13, type hints everywhere, `ruff` and `mypy --strict`. TypeScript: strict mode, ESLint, components in `<script setup>`.
8. Tests must not bind fixed host ports, so they can run beside the dev stack.
9. Add a dependency only when the change needs it, and pin it.

## Code style

Write code that is easy to find, read and change. When in doubt, write less.

1. **One job per file.** Keep source files and components under 300 lines where you can. A file over 500 lines must be split by responsibility. Test files follow the module they test (`tests/unit/<package>/test_<module>.py`) and split by behavior past 800 lines. ESLint enforces the 500-line limit in `frontend/app/`.
2. **Names you can search for.** Name files, functions and components after the concept they own, so a search for "route" or "certificate" finds them. Do not add catch-all modules such as `helpers.py`, `common.ts` or `misc/`. `homeproxy.util` holds only cross-cutting primitives such as `run`, atomic writes and redaction.
3. **Short, flat functions.** One function does one thing. Return early instead of nesting. Ruff and ESLint enforce cyclomatic complexity 10. Split the function instead of raising the limit or adding `noqa`.
4. **No speculative code.** Do not add a base class, factory, registry, plugin hook, option or flag for a single case or a future nobody asked for. Repeat a few lines rather than build an early abstraction. Extract a helper at the third copy.
5. **Use what exists.** Before you write something, look for it in the codebase, the standard library, FastAPI, Pydantic or Nuxt UI. Do not wrap a library only to rename it. Keep values in the types the library returns.
6. **Clear boundaries.** Use Pydantic models at the API and file boundaries, and plain dataclasses or values inside. Never import another package's `_private` names. Modules have no side effects at import time.
7. **Errors.** Raise the package's own exception types. Catch only what you can handle at that point. Never use a bare `except` or swallow an error without logging it.
8. **Comments say why.** The code shows what it does. Public functions get a one-line docstring, longer only when the behavior needs it. Do not leave commented-out code, change notes or unowned TODOs.
9. **Frontend.** Pages compose components. Move logic that is reused, or longer than about 20 lines, out of a component into a composable in `app/composables/`. Style with Nuxt UI props and utility classes, not new CSS.

## Tests

Tests prove what the product really does. More tests are not better tests.

1. **Real scenarios only.** Each test covers a behavior in `docs/`, an issue, a rule in this file, or a bug that happened. Do not test cases that cannot happen in a one-user, LAN-only home lab, such as heavy load, many parallel clients or a hostile local process. Attacks through user input, such as config injection, auth bypass and leaked secrets, are real and stay tested.
2. **One owner per behavior.** Before you add a test, search for one that covers the behavior already. Extend that test instead of writing a second one. Test each rule at the lowest level that can show it: validation in model tests, HAProxy output in golden tests. A higher-level test only checks that the parts connect, and does not repeat the lower-level cases.
3. **Every case adds something.** Parametrize similar inputs. Each case must reach a different branch or rule, so drop near-duplicates.
4. **Test through public behavior.** Call public functions, the API or the real binary. Do not test private helpers. Mock only what a test cannot run, such as Cloudflare or the internet. Replace this project's own code only to inject a failure that a real run cannot easily produce, such as a crash in the middle of a write.
5. **A test must be able to fail.** Break the code under test and confirm that the test fails.
6. **Readable.** One behavior per test, named for that behavior. Put shared setup in fixtures in `conftest.py`. Do not use a fixed sleep to wait for something to start or finish. Poll for it with a timeout.

## Evidence

1. **Back every claim with evidence.** "Broken", "fixed", "passes" and "works" each need command output, a test result, a reproduction or a `file:line`. Without evidence, say it is unverified.
2. **Trust the diff, not the summary.** Review a code change by reading its diff. A report of what changed only points where to look.

## Writing

Write all text for people in plain English, with no filler. Write `docs/` and step-by-step text in ASD-STE100 Simplified Technical English. Quote UI labels exactly. A change to a UI label also changes its tests and the docs that quote it.

## Workflow

- `main` holds the latest release and is the default branch. All work goes to `dev`.
- Commit straight to `dev` by default. There is one maintainer, so a branch only adds steps.
- For a small issue, put `Closes #N` in the commit message. The release PR description repeats it, and that closes the issue when the release merges.
- Use a branch and a PR into `dev` (`gh pr create --base dev`) only for issue work that needs one, such as work over several sessions or work that needs a review. Put `Closes #N` in the PR description. The squash commit keeps it, so the release PR description finds it.
- Before a push, run `mise run check`. Also run `test-integration` when runtime behavior changes, `test-e2e` when the UI changes, and `image` when the image changes. User-visible behavior goes in `docs/`.
- Commit messages and PR titles use Conventional Commits, for example `feat(generator): add LAN-only map`.
- Sign off every commit with `git commit -s`. The DCO check fails a PR with a commit that has no `Signed-off-by` line.
- Never commit secrets, generated client output or local data.
- If your session gives you commit attribution lines, end each commit message with them.

To release, run `mise run bump <kind>` on a clean `dev`, commit and push. Then open a PR from `dev` into `main` with the `release` label and the description that the task writes. The description holds the release notes and a `Closes #N` line for each issue that a commit since the last release names, so the merge closes them. After a later push to `dev`, refresh it with `mise run release-notes --closes` and `gh pr edit`. The release gets the same notes, made again from the commits when it publishes. Commit subjects become release notes, so write them for users. Its checks run the integration and end-to-end tests, and they fail if the version is already released. Merge it with a merge commit, never a squash. The merge publishes the image and the GitHub release.

## The AGENTS directory

`AGENTS/` at the repository root is for working notes and is not in git. Use it for a plan or a handover that is too large for an issue. Use one file for each piece of work, named `<issue number>-<slug>.md` or `<slug>.md`, with these sections:

- Goal: the issue link and the result you want.
- Plan: the steps, as a checklist.
- Findings: facts you found, with evidence.
- Handover: what is done, the next step and the open questions.

Before the work closes, move the decisions to the issue or the PR, and move the behavior to the code, the tests or `docs/`. Then delete the file. A small task from an issue needs no file.
