import type { Page } from "@playwright/test";
import { apply, expect, logIn, signIn, test } from "./fixtures";
import { viaHaproxy } from "./stack";

// The flows build on each other in one stack: a domain, a site, its paths, then site settings, each as HAProxy serves it.
test.describe.configure({ mode: "serial" });

const SITE = "http://app.example.com";

/** Open one path of a site in the editor from its row on the Routes page, as the admin does. */
async function openPath(page: Page, host: string, path: string): Promise<void> {
  await page.goto("/routes");
  await page.getByRole("region", { name: host, exact: true }).getByRole("link", { name: new RegExp(`^${path}\\s*→`) }).click();
}

test("add a domain without a certificate", async ({ page, stack }) => {
  await signIn(page, stack);

  await page.goto("/domains");
  await page.getByRole("link", { name: "Add domain" }).first().click();
  await page.getByLabel("Domain", { exact: true }).fill("example.com");
  await expect(page.getByRole("radio", { name: /No certificate/ })).toBeChecked();
  await page.getByRole("button", { name: "Stage change" }).click();
  await expect(page.getByRole("region", { name: "example.com" })).toContainText("No certificate");
});

test("add a site from a pasted URL, apply it from Details, and HAProxy serves it with its health", async ({ page, stack }) => {
  await logIn(page);
  await page.goto("/routes");
  await page.getByRole("link", { name: "Add site" }).first().click();
  await page.getByLabel("Hosts").fill("app.example.com");
  await page.getByLabel("Hosts").press("Enter");
  await page.getByLabel("Upstreams").fill(`http://${stack.upstream}/`);
  await page.getByLabel("Upstreams").press("Enter");
  await expect(page.getByText(`${stack.upstream}:80`)).toBeVisible();
  await page.getByRole("button", { name: "Stage change" }).click();

  await expect(page).toHaveURL(/\/routes$/);
  const bar = page.getByTestId("pending-bar");
  await expect(bar).toContainText("Route app.example.com added");
  await bar.getByRole("button", { name: "Details" }).click();
  const details = page.getByRole("dialog");
  await details.getByRole("button", { name: "Configuration files" }).click();
  await expect(details.getByText(`address: ${stack.upstream}:80`)).toBeVisible();
  await details.getByRole("button", { name: "Apply" }).click();
  await expect(page.getByText("Applied", { exact: true })).toBeVisible({ timeout: 60_000 });
  await expect(bar).toHaveCount(0);

  const answer = await viaHaproxy(stack, `${SITE}/`);
  expect(answer.status).toBe(200);
  expect(answer.body).toContain("GET / HTTP/1.1");
  const site = page.getByRole("region", { name: "app.example.com" });
  await expect(site.getByRole("img", { name: "Up" }).first()).toBeVisible({ timeout: 30_000 });
});

test("on a phone, Overview with a site fits the screen and the menu opens the navigation", async ({ page }) => {
  await logIn(page);
  await page.goto("/");
  const health = page.getByRole("region", { name: "Route health" });
  const requests = page.getByRole("region", { name: "Requests, last 24 hours" });
  await expect(health).toContainText("app.example.com");
  // Drawn wide first, as on a phone turned upright: the chart held its grid column open (214b589).
  // It has no accessible name, so its drawing is awaited by its element.
  await expect(requests.locator("svg").first()).toBeVisible();
  await page.setViewportSize({ width: 375, height: 700 });
  for (const card of [requests, health]) {
    await expect.poll(async () => {
      const box = await card.boundingBox();
      return (box?.x ?? 0) + (box?.width ?? Infinity);
    }).toBeLessThanOrEqual(375);
  }
  await page.getByRole("button", { name: "Open sidebar" }).click();
  await expect(page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "Routes" })).toBeVisible();
});

test("a path with a stripped prefix reaches its upstream without the prefix", async ({ page, stack }) => {
  await logIn(page);
  await page.goto("/routes");
  await page.getByRole("region", { name: "app.example.com" }).getByRole("link", { name: "Add path" }).click();
  await expect(page.getByRole("heading", { name: "Add path to app.example.com" })).toBeVisible();
  await page.getByLabel("Path", { exact: true }).fill("/api");
  await page.getByLabel("Upstreams").fill(`${stack.upstream}:80`);
  await page.getByLabel("Upstreams").press("Enter");
  await page.getByRole("button", { name: "Advanced" }).click();
  await page.getByRole("switch", { name: "Remove /api before forwarding" }).click();
  await page.getByRole("button", { name: "Stage change" }).click();
  await apply(page);

  expect((await viaHaproxy(stack, `${SITE}/api/x?q=1`)).body).toContain("GET /x?q=1 HTTP/1.1");
  expect((await viaHaproxy(stack, `${SITE}/other`)).body).toContain("GET /other HTTP/1.1");
});

test("a password keeps its stored value on an edit, and a new one is staged alone without showing its hash", async ({ page, stack }) => {
  await logIn(page);
  await page.goto("/routes");
  await page.getByRole("region", { name: "app.example.com" }).getByRole("link", { name: "Add path" }).click();
  await page.getByLabel("Path", { exact: true }).fill("/admin");
  await page.getByLabel("Upstreams").fill(`${stack.upstream}:80`);
  await page.getByLabel("Upstreams").press("Enter");
  await page.getByRole("switch", { name: /Ask for a password/ }).click();
  await page.getByRole("button", { name: "Add user" }).click();
  await page.getByLabel("User name").fill("alice");
  await page.getByLabel("User password").fill("first user password");
  await page.getByRole("button", { name: "Stage change" }).click();
  await expect(page).toHaveURL(/\/routes$/);
  await apply(page);
  expect((await viaHaproxy(stack, `${SITE}/admin`)).status).toBe(401);
  expect((await viaHaproxy(stack, `${SITE}/admin`, "alice:first user password")).status).toBe(200);

  // An edit that leaves the password field empty keeps the stored password.
  await openPath(page, "app.example.com", "/admin");
  await expect(page.getByLabel("User name")).toHaveValue("alice");
  await expect(page.getByLabel("User password")).toHaveValue("");
  await page.getByRole("button", { name: "Advanced" }).click();
  await page.getByRole("switch", { name: "WebSocket" }).click();
  await page.getByRole("button", { name: "Stage change" }).click();
  await apply(page);
  expect((await viaHaproxy(stack, `${SITE}/admin`, "alice:first user password")).status).toBe(200);

  // A new password alone changes no staged file: the bar names it and has nothing to discard.
  await openPath(page, "app.example.com", "/admin");
  await page.getByLabel("User password").fill("second user password");
  await page.getByRole("button", { name: "Stage change" }).click();
  await expect(page.getByText("Basic auth users saved", { exact: true })).toBeVisible();
  const bar = page.getByTestId("pending-bar");
  await expect(bar).toContainText("Basic auth users changed on app.example.com");
  await expect(bar.getByRole("button", { name: "Discard" })).toHaveCount(0);
  await bar.getByRole("button", { name: "Details" }).click();
  const details = page.getByRole("dialog");
  await expect(details).toContainText("Basic auth users changed on app.example.com");
  // The diffs are collapsed: open every file section the dialog offers before looking for secrets.
  for (const section of await details.getByRole("button", { name: /files$/ }).all()) await section.click();
  await expect(details).not.toContainText("second user password");
  await expect(details).not.toContainText("$6$");
  await page.keyboard.press("Escape");
  await apply(page);
  expect((await viaHaproxy(stack, `${SITE}/admin`, "alice:first user password")).status).toBe(401);
  expect((await viaHaproxy(stack, `${SITE}/admin`, "alice:second user password")).status).toBe(200);
});

test("headers set in the editor reach the upstream and the client", async ({ page, stack }) => {
  await logIn(page);
  await openPath(page, "app.example.com", "/");
  await page.getByRole("button", { name: "Advanced" }).click();
  await page.getByRole("button", { name: "Add request header" }).click();
  await page.getByLabel("request header name").fill("X-Forwarded-Prefix");
  await page.getByLabel("request header value").fill("/app");
  await page.getByRole("button", { name: "Add response header" }).click();
  await page.getByLabel("response header name").fill("X-Frame-Options");
  await page.getByLabel("response header value").fill("SAMEORIGIN");
  await page.getByRole("button", { name: "Stage change" }).click();
  await apply(page);

  const answer = await viaHaproxy(stack, `${SITE}/`);
  expect(answer.body).toContain("X-Forwarded-Prefix: /app");
  expect(answer.headers["x-frame-options"]).toBe("SAMEORIGIN");
});

test("a redirect answers with its code and keeps the path and query", async ({ page, stack }) => {
  await logIn(page);
  await page.goto("/routes");
  await page.getByRole("link", { name: "Add site" }).first().click();
  await page.getByRole("radio", { name: /Redirect/ }).click();
  await page.getByLabel("Hosts").fill("www.example.com");
  await page.getByLabel("Hosts").press("Enter");
  await page.getByLabel("URL").fill("http://app.example.com");
  await page.getByLabel("Kind").click();
  await page.getByRole("option", { name: /^308/ }).click();
  await page.getByRole("switch", { name: "Keep the path and query" }).click();
  await page.getByRole("button", { name: "Stage change" }).click();
  await apply(page);

  const answer = await viaHaproxy(stack, "http://www.example.com/a?b=1");
  expect(answer.status).toBe(308);
  expect(answer.headers.location).toBe("http://app.example.com/a?b=1");
});

test("a site setting changed in one path's editor is staged on every path of the site", async ({ page, stack }) => {
  await logIn(page);
  await openPath(page, "app.example.com", "/api");
  await expect(page.getByText("Applies to every path of the site: this one and /, /admin")).toBeVisible();
  await page.getByRole("radio", { name: /Another address/ }).click();
  await page.getByLabel("Address", { exact: true }).fill("192.168.1.30");
  await expect(page.getByText("Stage change also updates /, /admin")).toBeVisible();
  await page.getByRole("button", { name: "Stage change" }).click();

  await expect(page.getByText("Also updated", { exact: true })).toBeVisible();
  await expect(page.getByTestId("pending-bar")).toContainText("3 changes staged");
  await openPath(page, "app.example.com", "/");
  await expect(page.getByRole("radio", { name: /Another address/ })).toBeChecked();
  await expect(page.getByLabel("Address", { exact: true })).toHaveValue("192.168.1.30");
  await page.getByRole("link", { name: "Cancel" }).click();
  await apply(page);
  expect((await viaHaproxy(stack, `${SITE}/`)).status).toBe(200);
});

test("a route country list that replaces the global one saves, even empty, and says so", async ({ page }) => {
  await logIn(page);
  await openPath(page, "app.example.com", "/");
  await page.getByRole("switch", { name: /Block countries/ }).click();
  await expect(page.getByText("Countries (adds to the global list)")).toBeVisible();
  await page.getByRole("switch", { name: /Replace the global list/ }).click();
  await expect(page.getByText("Countries (replaces the global list)")).toBeVisible();
  await page.getByRole("button", { name: "Stage change" }).click();
  await apply(page);

  await openPath(page, "app.example.com", "/");
  await expect(page.getByRole("switch", { name: /Replace the global list/ })).toBeChecked();
  await expect(page.getByText("Countries (replaces the global list)")).toBeVisible();
});
