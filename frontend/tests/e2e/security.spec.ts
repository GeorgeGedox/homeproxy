import type { Page } from "@playwright/test";
import { expect, test } from "./fixtures";

const soon = new Date(Date.now() + 3 * 3600_000).toISOString();
const decision = (id: number, value: string, origin: string) => ({ id, value, origin, type: "ban", scope: "Ip", scenario: "crowdsecurity/ssh-bf", expires_at: soon });
const geoip = { provider: "dbip-lite", block_countries: [], data: null, attribution: { text: "IP Geolocation by DB-IP", url: "https://db-ip.com" } };

// A real stack has no CrowdSec LAPI and no internet for GeoIP, so these run against a mock.

/** The Security page's API with CrowdSec on: `extra` replies win, keyed "METHOD /path". */
async function mockSecurity(page: Page, extra: Record<string, (body: unknown) => unknown> = {}) {
  await page.route("**/api/v1/**", (request) => {
    const req = request.request();
    const path = new URL(req.url()).pathname.replace("/api/v1", "");
    const key = `${req.method()} ${path}`;
    const defaults: Record<string, unknown> = {
      "GET /auth/me": { authenticated: true }, "GET /security/allowlist": [], "GET /geoip": geoip, "GET /jobs": [],
      "GET /changes": { changes: [], summary: [] }, "GET /status": { bouncer: "up" },
    };
    const handler = extra[key];
    const reply = handler ? handler(req.postDataJSON()) : defaults[key];
    return request.fulfill({ status: reply === undefined ? 404 : 200, contentType: "application/json", body: reply === null ? "" : JSON.stringify(reply ?? {}) });
  });
}

test("blocklist decisions are hidden until asked for, and unbanning one offers the allowlist", async ({ page }) => {
  let decisions = [decision(1, "203.0.113.7", "crowdsec"), decision(2, "198.51.100.9", "CAPI")];
  const calls: string[] = [];
  const drop = (id: number) => (decisions = decisions.filter((item) => item.id !== id));
  await mockSecurity(page, {
    "GET /security/decisions": () => decisions,
    "POST /security/allowlist": (body) => (calls.push(`allow ${(body as { entry: string }).entry}`), { allowlist: [], warnings: [] }),
    "DELETE /security/decisions/1": () => (calls.push("delete 1"), drop(1), null),
    "DELETE /security/decisions/2": () => (calls.push("delete 2"), drop(2), null),
  });
  await page.goto("/security");
  const list = page.getByRole("list", { name: "Decisions" });
  await expect(list).toContainText("203.0.113.7");
  await expect(list).not.toContainText("198.51.100.9");

  await page.getByRole("button", { name: "Unban 203.0.113.7" }).click();
  await expect.poll(() => calls).toEqual(["delete 1"]);
  await expect(page.getByText("Nothing detected here or added by hand.")).toBeVisible();

  // A search finds a blocklist address without showing them all.
  await page.getByLabel("Find an address").fill("198.51");
  await page.getByRole("button", { name: "Unban 198.51.100.9" }).click();
  const dialog = page.getByRole("dialog", { name: "198.51.100.9 is on a blocklist" });
  await expect(dialog).toContainText("may come back");
  await dialog.getByRole("button", { name: "Allowlist and unban" }).click();
  await expect.poll(() => calls).toEqual(["delete 1", "allow 198.51.100.9", "delete 2"]);
  await page.getByLabel("Find an address").fill("");
  await expect(page.getByText("Nobody is banned right now.")).toBeVisible();
});

test("when the unban fails after the allowlist, the allowlist shows the address and the error says the decision stayed", async ({ page }) => {
  const allowlist: string[] = [];
  await mockSecurity(page, {
    "GET /security/decisions": () => [decision(2, "198.51.100.9", "CAPI")],
    "GET /security/allowlist": () => allowlist,
    "POST /security/allowlist": (body) => (allowlist.push((body as { entry: string }).entry), { allowlist, warnings: [] }),
  });
  await page.goto("/security");
  await page.getByLabel("Find an address").fill("198.51");
  await page.getByRole("button", { name: "Unban 198.51.100.9" }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Allowlist and unban" }).click();

  await expect(page.getByText("The address is allowlisted, but its decision was not removed.")).toBeVisible();
  await expect(page.getByRole("region", { name: "Always allowed" })).toContainText("198.51.100.9");
});

test("a refused ban keeps the dialog open with the reason", async ({ page }) => {
  const message = "The CrowdSec API refused the decision: value is not an address or a range.";
  await mockSecurity(page, { "GET /security/decisions": () => [] });
  // Registered last, so it answers before the mock.
  await page.route("**/api/v1/security/decisions", (request) => request.request().method() === "POST"
    ? request.fulfill({ status: 502, contentType: "application/json", body: JSON.stringify({ error: { code: "crowdsec_error", message, field: null } }) })
    : request.fallback());
  await page.goto("/security");
  await page.getByRole("button", { name: "Ban an address" }).click();
  const dialog = page.getByRole("dialog", { name: "Ban an address" });
  await dialog.getByLabel("Address or range").fill("203.0.113.7");
  await dialog.getByRole("button", { name: "Ban", exact: true }).click();
  await expect(dialog).toContainText(message);
});

test("extend offers only longer durations, and replaces the decision with the longer one", async ({ page }) => {
  let decisions = [decision(1, "203.0.113.7", "crowdsec")];
  const added: unknown[] = [];
  await mockSecurity(page, {
    "GET /security/decisions": () => decisions,
    "POST /security/decisions": (body) => {
      added.push(body);
      decisions = [...decisions, { ...decision(3, "203.0.113.7", "cscli"), expires_at: new Date(Date.now() + 7 * 86400_000).toISOString() }];
      return null;
    },
    "DELETE /security/decisions/1": () => ((decisions = decisions.filter((item) => item.id !== 1)), null),
  });
  await page.goto("/security");
  await page.getByRole("button", { name: "More for 203.0.113.7" }).click();
  await page.getByRole("menuitem", { name: "Extend" }).hover();
  // Only once the submenu is open does a missing "1 hour" mean it was left out.
  await expect(page.getByRole("menuitem", { name: "7 days" })).toBeVisible();
  await expect(page.getByRole("menuitem", { name: "1 hour" })).toHaveCount(0);
  await page.getByRole("menuitem", { name: "7 days" }).click();
  await expect.poll(() => added).toEqual([{ value: "203.0.113.7", type: "ban", duration_seconds: 604800 }]);
  await expect(page.getByRole("listitem", { name: "203.0.113.7" })).toHaveCount(1);
  await expect(page.getByRole("listitem", { name: "203.0.113.7" })).toContainText("Added by hand");
});

test("a GeoIP refresh shows while it runs, then the new data, or the error of a failed one", async ({ page }) => {
  let state = "running";
  const job = () => ({ id: "g1", kind: "geoip.refresh", subject: null, state, error: state === "failed" ? "download.maxmind.com answered HTTP 401" : null, created_at: soon });
  const data = { provider: "dbip-lite", entries: 393278, updated_at: new Date().toISOString(), countries: ["CN", "DE"] };
  await mockSecurity(page, {
    "GET /security/decisions": () => [],
    "POST /geoip/refresh": () => ((state = "running"), job()),
    "GET /jobs/g1": () => job(),
    "GET /geoip": () => ({ ...geoip, data: state === "finished" ? data : null }),
  });
  await page.goto("/security");
  const countries = page.getByRole("region", { name: "Countries" });
  await expect(countries).toContainText("No data yet");
  await countries.getByRole("button", { name: "Refresh data" }).click();
  await expect(countries).toContainText("Downloading…");
  state = "finished";
  await expect(countries).toContainText("393,278 ranges");
  await expect(page.getByRole("list", { name: "Countries not blocked" })).toContainText("Germany");

  await countries.getByRole("button", { name: "Refresh data" }).click();
  // The refresh request sets the job running, so fail it only after that request was answered.
  await expect(countries).toContainText("Downloading…");
  state = "failed";
  await expect(countries).toContainText("The download failed: download.maxmind.com answered HTTP 401");
});
