import { test as base, expect, type Page } from "@playwright/test";
import { savedStacks, setupToken, stackLog, type Stack } from "./stack";

export const PASSWORD = "a long e2e admin password";

/**
 * Playwright test against the built image. Each worker uses the stack of its parallel index, which
 * the setup project started, and a worker that restarts after a failure keeps it. The tests of a file
 * share it in order, after the files that ran on it before.
 */
export const test = base.extend<object, { stack: Stack }>({
  stack: [
    // eslint-disable-next-line no-empty-pattern
    async ({}, use, workerInfo) => {
      const stack = savedStacks()[workerInfo.parallelIndex];
      if (!stack) throw new Error(`No stack for worker ${workerInfo.parallelIndex}. Run the real project, which starts them first.`);
      await use(stack);
    },
    { scope: "worker" },
  ],
  baseURL: async ({ stack }, use) => {
    await use(stack.dashboard);
  },
  // A failed test prints the requests that failed and the container log.
  page: async ({ page, stack }, use, testInfo) => {
    const failed: string[] = [];
    page.on("requestfailed", (request) => failed.push(`${request.method()} ${request.url()}: ${request.failure()?.errorText}`));
    await use(page);
    if (testInfo.status === testInfo.expectedStatus) return;
    const tail = stackLog(stack).split("\n").slice(-60).join("\n");
    console.log(`Failed requests:\n${failed.join("\n") || "none"}\n--- container log, last 60 lines ---\n${tail}`);
  },
});

/** Sign in to the stack: through the setup wizard on a new one, else through the login page. */
export async function signIn(page: Page, stack: Stack): Promise<void> {
  // A new browser context has no session, so the app always sends it to setup or to the login page.
  await page.goto("/");
  await page.waitForURL(/\/(setup|login)$/);
  if (page.url().endsWith("/login")) return logIn(page);
  // Setup shows what it found and will write, the dashboard address included.
  await expect(page.getByTestId("setup-summary")).toContainText("LAN ranges");
  await expect(page.getByTestId("setup-summary")).toContainText("DNS server");
  await expect(page.getByTestId("setup-summary")).toContainText("Dashboard");
  await page.getByLabel("Setup token").fill(await setupToken(stack));
  await page.getByLabel("Admin password").fill(PASSWORD);
  await page.getByLabel("Repeat the password").fill(PASSWORD);
  await page.getByRole("button", { name: "Finish setup" }).click();
  await expect(page).toHaveURL(/\/$/, { timeout: 60_000 });
}

/** Log in through the login page. */
export async function logIn(page: Page): Promise<void> {
  await page.goto("/login");
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Log in" }).click();
  await expect(page).not.toHaveURL(/\/login/);
}

/** Stage a domain without a certificate through the Domains page. */
export async function addDomain(page: Page, zone: string): Promise<void> {
  await page.goto("/domains");
  await page.getByRole("link", { name: "Add domain" }).first().click();
  await page.getByLabel("Domain", { exact: true }).fill(zone);
  await page.getByRole("button", { name: "Stage change" }).click();
  await expect(page).toHaveURL(/\/domains$/);
}

/** Stage a site that sends `host`, or one path of it, to the stack's upstream, through the Routes page. */
export async function addSite(page: Page, stack: Stack, host: string, path?: string): Promise<void> {
  await page.goto("/routes");
  await page.getByRole("link", { name: "Add site" }).first().click();
  await page.getByLabel("Hosts").fill(host);
  await page.getByLabel("Hosts").press("Enter");
  if (path) await page.getByLabel("Path", { exact: true }).fill(path);
  await page.getByLabel("Upstreams").fill(`${stack.upstream}:80`);
  await page.getByLabel("Upstreams").press("Enter");
  await page.getByRole("button", { name: "Stage change" }).click();
  await expect(page).toHaveURL(/\/routes$/);
}

/** Apply from the pending bar and wait until nothing is staged. */
export async function apply(page: Page): Promise<void> {
  await page.getByTestId("pending-bar").getByRole("button", { name: "Apply" }).click();
  await expect(page.getByTestId("pending-bar")).toHaveCount(0, { timeout: 60_000 });
}

export { expect };
