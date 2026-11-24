import type { Page } from "@playwright/test";
import { expect, routeApi, test } from "./fixtures";

interface Mock {
  /** The last certificate issue failed. */
  issueFailed?: boolean;
  versionCheck?: Record<string, unknown>;
}

/**
 * The Overview's reads. A real stack has no CrowdSec, no internet for the version check, and no
 * DNS server that has run for hours, so these states come from a mock.
 */
async function mockApi(page: Page, mock: Mock = {}): Promise<void> {
  const replies: Record<string, unknown> = {
    "/setup/status": { setup_required: false },
    "/auth/me": { authenticated: true },
    "/changes": { changes: [], summary: [], conflict: false, conflicting_paths: [], config_diff: "", generated_diff: "", generated_error: null },
    "/status": {
      haproxy_version: "3.4.7", haproxy_error: null, haproxy_uptime_seconds: 60, version_check: mock.versionCheck ?? null, update_check: null,
      update_check_configured: false, bouncer: "up", dns_listening: true, dns_uptime_seconds: 7200, last_apply: null,
      routes: [{ name: "app", status: "UP", requests: 0, responses_5xx: 0 }],
    },
    "/security/decisions/counts": { bans: 2, captchas: 0, blocklist: true },
    "/routes": [{ name: "app", type: "http", hosts: ["app.example.com"], path_prefix: "/", exposure: "lan", upstreams: [], enabled: true }],
    "/domains": [{ zone: "example.com", wildcard_cert: true, dns_provider: "cloudflare", credentials: "cf" }],
    "/certificates": [{ name: "example.com", issuer: "R11", names: ["example.com"], not_after: "2027-01-01T00:00:00Z", days_left: 52, state: "ok" }],
    "/jobs": mock.issueFailed
      ? [{ id: "j1", kind: "certificate.issue", subject: "example.com", state: "failed", error: "DNS challenge timed out", created_at: "2026-11-22T10:00:00Z" }]
      : [],
  };
  await routeApi(page, (_, path) => (path in replies ? [200, replies[path]] : undefined));
}

test("the overview shows route health, certificate expiry, the DNS uptime and your own bans", async ({ page }) => {
  await mockApi(page);
  await page.goto("/");
  await expect(page.getByRole("region", { name: "Route health" }).getByRole("img", { name: "Up" })).toBeVisible();
  await expect(page.getByText("app.example.com")).toBeVisible();
  await expect(page.getByText("52d left")).toBeVisible();
  await expect(page.getByText("Up 2h", { exact: true })).toBeVisible();
  await expect(page.getByText("2 bans · community list")).toBeVisible();
});

test("a problem under Needs attention links to the page that fixes it", async ({ page }) => {
  await mockApi(page, { issueFailed: true });
  await page.goto("/");
  await page.getByRole("link", { name: "A certificate job for example.com failed: DNS challenge timed out" }).click();
  await expect(page).toHaveURL(/\/domains$/);
});

test("the overview shows the latest HAProxy LTS patch and the LTS end date", async ({ page }) => {
  await mockApi(page, { versionCheck: { running: "3.4.7", branch: "3.4", latest: "3.4.9", eol: "2031-04-01", checked_at: "2026-11-10T00:00:00Z" } });
  await page.goto("/");
  await expect(page.getByText("Latest 3.4.9 · LTS until Apr 2031")).toBeVisible();
});
