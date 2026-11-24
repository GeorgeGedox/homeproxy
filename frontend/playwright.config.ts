import { defineConfig } from "@playwright/test";
import { STACKS } from "./tests/real/stack";

// `mocked` serves the built app from a static server on a free port and mocks the API, for error
// states a real run cannot produce. `real` drives the built image through its own dashboard, on
// random loopback ports (tests/real/stack.ts), one stack per worker. `stacks` starts them all before
// any browser and `stacks teardown` removes them after the last: a host network that changes under a
// loading page fails it, so `mise run test-e2e` also runs `mocked` before the stacks start.
export default defineConfig({
  reporter: "list",
  // Playwright's default is half the CPUs, 2 on a CI runner, which would leave stacks unused. One
  // worker per stack runs the real suite about 15% faster there (#42).
  workers: STACKS,
  use: { browserName: "chromium" },
  projects: [
    { name: "mocked", testDir: "tests/e2e", fullyParallel: true },
    { name: "stacks", testDir: "tests/real", testMatch: "stacks.setup.ts", teardown: "stacks teardown" },
    { name: "stacks teardown", testDir: "tests/real", testMatch: "stacks.teardown.ts" },
    { name: "real", testDir: "tests/real", dependencies: ["stacks"], workers: STACKS, timeout: 120_000, use: { ignoreHTTPSErrors: true } },
  ],
});
