import { readdirSync, readFileSync } from "node:fs";
import { expect, routeApi, test } from "./fixtures";

// Sign-in, logout and the phone layout run in tests/real. These need a session that runs out mid-page,
// an API that does not answer, or a check of every request the page makes.
test("a session that runs out mid-page sends the admin to log in, where a wrong password still says so", async ({ page }) => {
  let expired = false;
  await routeApi(page, (_, path) => {
    if (path === "/setup/status") return [200, { setup_required: false }];
    if (path === "/auth/me") return expired ? [401, {}] : [200, { authenticated: true }];
    if (path === "/auth/login") return [401, { error: { code: "invalid_password", message: "The password is wrong." } }];
    // The session runs out after the guard let the page in: its first data request is refused.
    expired = true;
    return [401, { error: { code: "unauthorized", message: "Log in to continue." } }];
  });
  await page.goto("/routes");
  await expect(page).toHaveURL(/\/login$/);
  await page.getByLabel("Password").fill("not-the-password");
  await page.getByRole("button", { name: "Log in" }).click();
  await expect(page.getByText("Wrong password.")).toBeVisible();
  await expect(page).toHaveURL(/\/login$/);
});

test("with the API not answering, a page says so instead of going blank", async ({ page }) => {
  await page.route("**/api/v1/**", (route) => route.abort("connectionrefused"));
  await page.goto("/routes");
  await expect(page).toHaveURL(/\/routes$/);
  await expect(page.getByText("The server is not reachable.")).toBeVisible();
});

test("renders dark only, offers no theme toggle and stays on the origin", async ({ page, appURL }) => {
  const foreign: string[] = [];
  const iconErrors: string[] = [];
  page.on("console", (msg) => {
    if (msg.text().includes("[Icon]")) iconErrors.push(msg.text());
  });
  page.on("response", (res) => {
    if (res.status() >= 400 && res.url().includes("_nuxt_icon")) iconErrors.push(res.url());
  });
  page.on("request", (req) => {
    const url = req.url();
    if (!url.startsWith(appURL) && !url.startsWith("data:") && !url.startsWith("blob:")) foreign.push(url);
  });
  await routeApi(page, (_, path) => (path === "/auth/me" ? [200, { authenticated: true }] : undefined));
  await page.emulateMedia({ colorScheme: "light" });
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Overview" })).toBeVisible();
  await expect(page.locator("html")).toHaveClass(/dark/);
  // Every nav link paints a glyph. An empty icon span has no path, so it fails here.
  const links = page.getByRole("navigation", { name: "Main" }).getByRole("link");
  const count = await links.count();
  expect(count).toBeGreaterThan(0);
  for (let i = 0; i < count; i++) {
    await expect(links.nth(i).locator("svg path, svg line, svg circle, svg rect").first()).toBeAttached();
    const box = await links.nth(i).locator("svg").first().boundingBox();
    expect(box?.width ?? 0).toBeGreaterThan(0);
  }
  await expect(page.getByTestId("theme-toggle")).toHaveCount(0);
  await expect(page.getByRole("button", { name: /theme|light|dark/i })).toHaveCount(0);
  await page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "Routes" }).click();
  await expect(page.getByRole("heading", { name: "Routes" })).toBeVisible();
  await page.waitForLoadState("networkidle");
  expect(foreign).toEqual([]);
  expect(iconErrors).toEqual([]);
});

test("built CSS references no remote URL or font CDN", () => {
  const dir = new URL("../../.output/public/_nuxt/", import.meta.url);
  const css = readdirSync(dir).filter((f) => f.endsWith(".css"));
  expect(css.length).toBeGreaterThan(0);
  for (const file of css) {
    const text = readFileSync(new URL(file, dir), "utf8");
    expect(text, file).not.toMatch(/url\(\s*["']?(https?:)?\/\//i);
    expect(text, file).not.toMatch(/fonts\.(googleapis|gstatic|bunny)|fontshare|fontsource/i);
  }
});
