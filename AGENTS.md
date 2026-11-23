# HomeProxy development instructions

HomeProxy is a self-hosted reverse proxy manager for home labs: one container that bundles HAProxy with a small LAN-only dashboard. HAProxy does all the proxying. The dashboard writes config and talks to HAProxy's sockets.

## Sources of truth

| Question | Read |
| --- | --- |
| What to build and how it behaves | [PRODUCT_DESIGN.md](PRODUCT_DESIGN.md) |
| Contracts: config files, data directory, processes, API | [PLAN.md](PLAN.md) |
| Why it is designed this way | [docs/DESIGN_PLAN.md](docs/DESIGN_PLAN.md) |
| Decisions made during the project | [docs/decisions.md](docs/decisions.md) |

If documents disagree, `PLAN.md` wins on contracts, `PRODUCT_DESIGN.md` wins on behavior, and `docs/DESIGN_PLAN.md` loses to both.

## Commands

Development tools are pinned in `mise.toml`. Run `mise install` once.

| Command | Purpose |
| --- | --- |
| `mise run check` | Lint, types, unit and golden tests. Run before every commit to `main` |
| `mise run test-integration` | Full container tests in compose |
| `mise run test-e2e` | Build the image, then Playwright: real flows through the image's dashboard (`tests/real`) and mocked-API specs for error states (`tests/e2e`) |
| `mise run image` | Build the amd64 image and smoke-test it |
| `mise run dev` | Start the dev stack with its data volume as it is (empty on first start). `-- seed` loads the example data, `-- reset` deletes the data |
| `mise run api-client` | Regenerate the TypeScript client and JSON schema |
| `mise run golden-update` | Update golden files. Review the diff |

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

1. **Real scenarios only.** Each test covers a behavior in `PRODUCT_DESIGN.md`, a contract in `PLAN.md`, a rule in "Code rules" above, or a bug that happened. Do not test cases that cannot happen in a one-user, LAN-only home lab, such as heavy load, many parallel clients or a hostile local process (see `docs/decisions.md`). Attacks through user input, such as config injection, auth bypass and leaked secrets, are real and stay tested.
2. **One owner per behavior.** Before you add a test, search for one that covers the behavior already. Extend that test instead of writing a second one. Test each rule at the lowest level that can show it: validation in model tests, HAProxy output in golden tests. A higher-level test only checks that the parts connect, and does not repeat the lower-level cases.
3. **Every case adds something.** Parametrize similar inputs. Each case must reach a different branch or rule, so drop near-duplicates.
4. **Test through public behavior.** Call public functions, the API or the real binary. Do not test private helpers. Mock only what a test cannot run, such as Cloudflare or the internet. Replace this project's own code only to inject a failure that a real run cannot easily produce, such as a crash in the middle of a write.
5. **A test must be able to fail.** Break the code under test and confirm that the test fails.
6. **Readable.** One behavior per test, named for that behavior. Put shared setup in fixtures in `conftest.py`. Do not use a fixed sleep to wait for something to start or finish. Poll for it with a timeout.

## Evidence

1. **Back every claim with evidence.** "Broken", "fixed", "passes" and "works" each need command output, a test result, a reproduction or a `file:line`. Without evidence, say it is unverified.
2. **Trust the diff, not the summary.** Review a code change by reading its diff. A report of what changed only points where to look.

## Git rules

- Before a commit, run `mise run check`. Also run `test-integration` when runtime behavior changes, `test-e2e` when the UI changes, and `image` when the image changes. User-visible behavior goes in `docs/`.
- Commit messages use Conventional Commits, for example `feat(generator): add LAN-only map`.
- Never commit secrets, generated client output or local data.
- If your session gives you commit attribution lines, end each commit message with them.
