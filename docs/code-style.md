# Code style

This page tells contributors how to write and test HomeProxy code. The goal is code that is easy to find, read and change. When you are not sure, write less code.

The tools check what they can: ruff and mypy `--strict` for Python, ESLint and `vue-tsc` for the frontend, actionlint, shellcheck and hadolint for the infrastructure. `mise run check` runs all of them. This page gives the rules that the tools cannot check. The code rules in [AGENTS.md](../AGENTS.md#code-rules), for example no shell commands from user input and no secrets in logs, apply to all code.

## All code

1. **One job for each file.** Keep a source file or a component below 300 lines when you can. Split a file at 500 lines by responsibility. ESLint stops a file in `frontend/app/` at 500 lines.
2. **Names that you can find with a search.** Name files, functions and components after the concept that they own, so that a search for "route" or "certificate" finds them. Do not add catch-all modules such as `helpers.py`, `common.ts` or `misc/`. `homeproxy.util` holds only primitives that all packages use: `run`, atomic writes, redaction and HTTP.
3. **Short, flat functions.** A function does one thing. Return early, and do not nest. The tools stop a function at cyclomatic complexity 10. Split the function. Do not raise the limit, and do not add `noqa`.
4. **No speculative code.** Do not add a base class, a factory, a registry, a plugin hook, an option or a flag for one case. Do not add them for a future need. Repeat a few lines before you make an abstraction. Make a shared helper at the third copy.
5. **Use what exists.** Before you write something, look for it in this codebase, the standard library, FastAPI, Pydantic, Nuxt and Nuxt UI. Do not wrap a library only to change its name. Keep values in the types that the library gives.
6. **Clear boundaries.** Validate data at each boundary: the API, the files and the network. Inside a package, use plain values and dataclasses. Do not import the `_private` names of a different package.
7. **No work at import time.** A module can define constants and objects when it loads. It must not read files, use the network or change global state, such as warning filters. The ASGI `app` in `homeproxy/main.py` is the one exception, because the server loads it by name.
8. **Errors.** Raise the exception types of your own package. Catch only the errors that you can handle at that point. Do not use a bare `except` or an empty `catch`. Do not hide an error without a log line.
9. **Comments tell why.** The code shows what it does. A public function has a docstring of one line. Write more only when the behavior needs it. Do not keep code in comments, change notes or TODO lines. To record a known limit, write `# shortcut: <the limit>, <when to change it>`.
10. **Exceptions to a tool rule name their reason.** A `# noqa: CODE` or `# type: ignore[code]` names the code and gives its reason on the same line or in the comment above.

## Backend

The backend is Python 3.13 in `backend/homeproxy/`, one package for each area (see [AGENTS.md](../AGENTS.md#where-to-find-things)).

- **Packages.** A package `__init__.py` has a docstring that tells how its modules divide the work. When callers use only a few names, the `__init__.py` lists them in `__all__`.
- **Errors.** Each package has its own exception classes, in `errors.py` or next to the code that raises them. The message of an error must be safe to log and to return: it never holds content from a file or a secret. When you convert an error from a library, use `raise ... from None`, because the library error can hold that content.
- **API errors.** An API handler raises an HTTP error with a status, a code, a message and, for a form, the field. The answer body is always `{"error": {"code", "message", "field"}}`. A `ValidationError` reaches a client only through `safe_errors`.
- **Models.** Files and request bodies use `StrictModel`, which forbids extra keys, is strict and frozen, and hides input values in errors. Response models are plain `BaseModel` classes. Put a validator or a constraint that more than one model uses in `models/fields.py` as an `Annotated` type.
- **Routers.** One router for each area, with the prefix `/api/v1`. Each route sets `response_model` and documents its error answers in `responses`. Dependencies are `Annotated` aliases, such as `Runtime` and `CurrentSession`. Handlers are plain `def` with a docstring of one line, unless they must wait on something.
- **Logging.** Each module has `logger = logging.getLogger(__name__)`. Write messages in lowercase with `%` arguments, not f-strings. Log errors by type and short reason. Do not log request bodies or file content.
- **Processes, files and HTTP.** Run programs only with `homeproxy.util.run` and a fixed argument list. Write files only with `atomic_write`. Use `homeproxy.util.http.urlopen` for outbound HTTP; ruff stops `urllib.request.urlopen`.
- **Types.** Write an alias as `type X = ...` and an optional value as `X | None`. Mark a constant with `Final`. Use `Literal` or `StrEnum` for a fixed set of values.
- **Constants.** A URL, port or timeout is a named `UPPER_SNAKE` constant. When a test or the dev stack must change it, add a `*_ENV = "HOMEPROXY_..."` variable next to it. A function that reads the environment takes `environ: Mapping[str, str] = os.environ`, so that a test can give its own.
- **Imports.** Put imports at the top of the module. Import late only to break an import cycle, and add a comment that tells why.
- **Docstrings.** Write in the imperative: "Return the pending changes." Tell the errors in a sentence such as "Raises `StoreError` when the file is not valid YAML." Do not use `Args:` or `Returns:` sections. A module constant can have a docstring on the line below it.

## Frontend

The frontend is a Nuxt 4 app in `frontend/app/`, with Nuxt UI, as a static site (`ssr: false`). It has a dark theme only, and it makes no request outside its origin: fonts and icons are in the bundle.

- **Layout.**
  - A page is `pages/<area>/index.vue` or `pages/<area>/edit.vue`.
  - A component is `components/<area>/<Area><Thing>.vue`, for example `components/routes/RoutesFormSite.vue`, so that its Nuxt name is its file name.
  - A composable is `composables/use<Thing>.ts`, with one exported `use<Thing>` in each file.
  - Pure functions, types and constants go in `utils/<concept>.ts`, with explicit imports, so that Vitest can run them without Nuxt.
- **Composables or utils.** Code that calls `useApi`, `useState`, `useAsyncData`, `useToast` or a lifecycle hook is a composable. All other code is a util.
- **Pages and components.** Pages compose components. Move logic that more than one component uses, or that is longer than about 20 lines, to a composable.
- **Components.**
  - Use `<script setup lang="ts">`.
  - Declare props with a type, `defineProps<{ ... }>()`, and a `/** */` comment for each prop.
  - Declare events in the tuple form: `defineEmits<{ remove: [Route] }>()`.
  - Use `defineModel<T>({ required: true })` for `v-model`.
  - Name handlers with a verb, such as `save` or `remove`, and events with a verb or a past participle, such as `remove` or `saved`.
- **API calls.**
  - Use `useApi()`, which gives the typed openapi-fetch client.
  - Import schema types as `type X = components["schemas"]["X"]` from `~~/api/schema`, which `mise run api-client` makes.
  - Read the result as `{ data, error, response }`.
  - Show a failure with `apiErrorMessage(error, "The X could not be loaded.")`.
  - Show form field errors from the server with `useFormError`.
  - Show results that are not an error with `useToast()`.
- **State.** Shared state uses `useState("<kebab-key>")`. Data from the server uses `useAsyncData` and `refreshNuxtData`. Do not add a store library.
- **Styling.**
  - Use Nuxt UI components, their props and utility classes. Put theme changes in `app.config.ts`. Do not add `<style>` blocks.
  - `assets/css/main.css` holds only fonts, design tokens and base rules, each with a comment that tells why.
  - Write icon names as `lucide:<name>`.
- **Exported functions.** An exported util or composable has a `/** */` comment of one line. An async function declares its return type, for example `Promise<void>`.

## Tests

Tests prove what the product really does. More tests are not better tests.

1. **Real scenarios only.** Each test covers a behavior in `docs/`, an issue, a rule in [AGENTS.md](../AGENTS.md) or on this page, or a bug that happened. Do not test cases that cannot happen in a home lab with one user and a dashboard on the LAN only. Examples are heavy load, many parallel clients and a hostile local process. Attacks through user input, such as config injection, auth bypass and leaked secrets, are real. Keep them tested.
2. **One owner for each behavior.** Before you add a test, search for a test that covers the behavior. Extend that test. Do not write a second one. Test each rule at the lowest level that can show it: validation in model tests, HAProxy output in golden tests. A test at a higher level only checks that the parts connect. It does not repeat the cases of the lower level.
3. **Each case adds something.** Parametrize similar inputs. Each case must reach a different branch or rule. Remove cases that do the same thing.
4. **Test through public behavior.** Call public functions, the API or the real binary. Do not test private helpers or read private attributes. Mock only what a test cannot run, such as Cloudflare or the internet. Replace code of this project only to cause a failure that a real run cannot easily cause, such as a crash during a write.
5. **A test must be able to fail.** Break the code under test, and make sure that the test fails.
6. **Readable.** A test checks one behavior and has a name that tells that behavior. Put shared setup in fixtures. Do not use a fixed sleep to wait for something. Poll for it with a timeout.

### Backend tests

- **Layout.**
  - Unit tests are in `tests/unit/<package>/test_<package>_<module>.py`. Model tests are in `test_model_<module>.py`.
  - Tests that need Docker or a container are in `tests/integration/<area>/` with names that end in `_live.py`. The `integration` marker is set for them automatically. `mise run test-integration` runs them, and `mise run check` does not.
  - Golden cases are in `tests/golden/<case>/`. `mise run golden-update` writes their expected output. Read the diff before you commit it.
  - Split a test file by behavior when it is longer than 800 lines.
- **Names.** A test name is a sentence that tells the behavior, for example `test_a_hand_edit_of_a_staged_file_is_a_conflict`. Add a docstring only when the name cannot hold the context.
- **Shared setup.** Fixtures are in the `conftest.py` nearest to the tests that use them. Helpers that many tests use, such as `wait_until`, `free_port`, `NOWHERE` and `Receiver`, are in `tests/conftest.py`. Import them. Do not copy them.
- **The internet.** Tests never use the internet. A fake server on a loopback port, or `NOWHERE` through a `*_ENV` variable, takes its place.
- **Waiting.** Use `wait_until(condition, what, timeout=...)`.
- **Parameters.** Give each case an id: `pytest.param(..., id="kebab-case")` or `ids=`.
- **Monkeypatch.** Use it to make a constant smaller, such as a retry count, or to cause a failure. Do not use it to replace the code under test.

### Frontend tests

- **Unit tests** for utils are next to the source, as `app/utils/<concept>.test.ts`, and run in Vitest.
- **Mocked end-to-end tests** in `tests/e2e/` serve the built app and answer the API from a mock. Use them only for states that a real run cannot easily make, such as an API error. The header of each file tells why.
- **Real end-to-end tests** in `tests/real/` use the built image. Each worker gets its own stack. Make data through the UI, as a user does, not through the API.
- **Selectors.** Use `getByRole` first, then `getByLabel` and `getByText`. Use a test id only when nothing that the user sees identifies the element. Quote UI labels exactly, with `exact: true` when a shorter label also exists.
- **Waiting.** Use the web-first `expect`, `expect.poll` or `toPass`. Do not use `waitForTimeout`.
- **Shared setup.** Shared setup is in `fixtures.ts`, next to the tests.
