import type { Page } from "@playwright/test";
import { expect, routeApi, test, type Reply } from "./fixtures";

const route = {
  name: "app", type: "http", hosts: ["app.example.com"], exposure: "public", enabled: true,
  upstreams: [{ address: "192.168.1.10:8080" }], auth: { type: "none" }, request_headers: {}, response_headers: {}, cloudflare: { dns: false, proxied: null },
  path_prefix: "/",
};

/** The domain, apply and Cloudflare replies of the mock: status and body. */
function applyAndCloudflare(key: string, changes: unknown[]): Reply | undefined {
  const zones = [{ zone: "example.com", changes, conflicts: [], errors: [], records: [] }];
  const replies: Record<string, unknown> = {
    "GET /domains": [{ zone: "example.com", cloudflare_off_reason: null }],
    "GET /cloudflare/records": { enabled: true, ddns: [], last_reconcile: null, zones },
    "POST /apply": { success: true, stage: "done", errors: [], warnings: [], certificate_jobs: [], cloudflare_job: "cf1" },
    "GET /jobs/cf1": { id: "cf1", kind: "cloudflare.reconcile", state: "finished", created_at: "2026-11-10T10:00:00Z" },
  };
  return key in replies ? [200, replies[key]] : undefined;
}

/** A mock backend where the route's `cloudflare.dns` flag is pending until an apply makes it live. */
async function mockApi(page: Page) {
  let wanted = false;
  let live = false;
  const zoneChange = () => {
    if (wanted === live) return [];
    return [{ action: wanted ? "create" : "delete", name: "app.example.com", content: "home.example.com", route: "app", proxied: false }];
  };
  await routeApi<{ cloudflare_dns: boolean }>(page, (method, path, body) => {
    const key = `${method} ${path}`;
    if (path === "/auth/me") return [200, { authenticated: true }];
    if (path === "/settings") return [200, { cloudflare: { enabled: true }, dns_server: { listen: "192.168.1.10:5335" } }];
    if (key === "GET /routes") return [200, [{ ...route, cloudflare: { dns: wanted, proxied: null } }]];
    if (key === "PUT /routes/hosts/app.example.com") {
      wanted = body.cloudflare_dns;
      return [200, { routes: [], warnings: [] }];
    }
    if (key === "GET /changes") {
      const pending = wanted !== live;
      return [200, {
        changes: pending ? [{ path: "routes/app.yaml", kind: "write" }] : [], conflict: false, conflicting_paths: [],
        summary: pending ? [{ kind: "route", action: "changed", label: "app.example.com" }] : [],
        config_diff: pending ? "+cloudflare" : "", generated_diff: "", generated_error: null,
      }];
    }
    if (key === "POST /apply") live = wanted;
    return applyAndCloudflare(key, zoneChange());
  });
}

// Cloudflare needs its API, so the switch runs against a mock. Details shows the change the mock
// derives from what the switch sent.
test("the route switch creates the record on apply and deletes it when switched off", async ({ page }) => {
  await mockApi(page);
  await page.goto("/routes");
  await page.getByLabel("Cloudflare DNS record for app.example.com").click();
  await page.getByTestId("pending-bar").getByRole("button", { name: "Details" }).click();
  const details = page.getByRole("dialog");
  await expect(details.getByText("create: app.example.com to home.example.com")).toBeVisible();
  await details.getByRole("button", { name: "Apply" }).click();
  await expect(page.getByText("Applied", { exact: true })).toBeVisible();
  await expect(page.getByText("Cloudflare records not updated")).toHaveCount(0);

  await page.goto("/routes");
  await page.getByLabel("Cloudflare DNS record for app.example.com").click();
  await page.getByTestId("pending-bar").getByRole("button", { name: "Details" }).click();
  await expect(page.getByRole("dialog").getByText("delete: app.example.com")).toBeVisible();
});

/** The Cloudflare page's API: the reads it needs, plus `extra` replies, each a body or a function of the call count. */
async function mockCloudflarePage(page: Page, state: object, extra: Record<string, unknown> = {}) {
  const calls: Record<string, number> = {};
  const replies: Record<string, unknown> = {
    "GET /auth/me": { authenticated: true },
    "GET /cloudflare/records": { last_reconcile: null, ddns_checked_at: null, ddns: [], zones: [], ...state },
    "GET /routes": [],
    "GET /domains": [{ zone: "example.com", dns_provider: "cloudflare", cloudflare_off_reason: null }],
    "GET /settings": { cloudflare: { enabled: true, reconcile_interval: "15m" } },
    "GET /jobs": [],
    ...extra,
  };
  await routeApi(page, (method, path) => {
    const key = `${method} ${path}`;
    calls[key] = (calls[key] ?? 0) + 1;
    const reply = replies[key];
    const body = typeof reply === "function" ? reply(calls[key]) : reply;
    return body === undefined ? undefined : [200, body];
  });
  return calls;
}

const ddnsResult = (action: string, content: string) => ({ zone: "example.com", record: "home.example.com", action, content, error: "" });
const job = (id: string, state: string) => ({ id, kind: "cloudflare.ddns", subject: null, state, created_at: "2026-11-21T10:00:00Z" });

test("only a hand-made DDNS record can be adopted", async ({ page }) => {
  const ddns = [ddnsResult("hand-made", "203.0.113.7"), { ...ddnsResult("blocked", ""), zone: "example.net", record: "home.example.net", error: "two A records share the name" }];
  await mockCloudflarePage(page, { enabled: true, ddns, zones: [{ zone: "example.com" }, { zone: "example.net" }] });
  await page.goto("/cloudflare");
  await expect(page.getByRole("button", { name: "Adopt" })).toHaveCount(1);
  await expect(page.getByRole("region", { name: "example.com" })).toContainText("Made by hand");
  await expect(page.getByRole("region", { name: "example.net" })).toContainText("two A records share the name");
});

test("Adopt waits for the DDNS update it starts, then shows the record at the new address", async ({ page }) => {
  let adopted = false;
  const calls = await mockCloudflarePage(page, {}, {
    "GET /cloudflare/records": () => ({ enabled: true, last_reconcile: null, ddns_checked_at: null, zones: [{ zone: "example.com" }],
      ddns: [adopted ? ddnsResult("updated", "198.51.100.20") : ddnsResult("hand-made", "203.0.113.7")] }),
    "POST /cloudflare/ddns/example.com/adopt": () => ddnsResult("adopted", "203.0.113.7"),
    // The update the adoption queues has no subject, so the page tells it from the older run by its id.
    "GET /jobs": () => (calls["POST /cloudflare/ddns/example.com/adopt"] ? [job("new", "running"), job("old", "finished")] : [job("old", "finished")]),
    "GET /jobs/new": (count: number) => {
      adopted = count > 1;
      return job("new", adopted ? "finished" : "running");
    },
  });
  await page.goto("/cloudflare");
  await page.getByRole("button", { name: "Adopt" }).click();
  const row = page.getByRole("listitem", { name: "DDNS home.example.com" });
  await expect(row).toContainText("Moved to your address");
  await expect(row).toContainText("198.51.100.20");
  await expect(page.getByRole("button", { name: "Adopt" })).toHaveCount(0);
});

test("a sync that could not write a record shows the error in its zone", async ({ page }) => {
  const run = { at: new Date().toISOString(), error: "", zones: [{ zone: "example.com", errors: ["app.example.com: Cloudflare answered 403"] }] };
  let synced = false;
  await mockCloudflarePage(page, {}, {
    "GET /cloudflare/records": () => ({ enabled: true, ddns: [], ddns_checked_at: null, zones: [{ zone: "example.com" }], last_reconcile: synced ? run : null }),
    "POST /cloudflare/reconcile": () => {
      synced = true;
      return run;
    },
  });
  await page.goto("/cloudflare");
  await expect(page.getByRole("region", { name: "Cloudflare sync" })).toContainText("In sync");
  await page.getByRole("button", { name: "Sync now" }).click();
  await expect(page.getByRole("region", { name: "Cloudflare sync" })).toContainText("1 problem");
  await expect(page.getByRole("region", { name: "example.com" })).toContainText("app.example.com: Cloudflare answered 403");
});

test("site records can be removed while Cloudflare is off, and the page says what went and what failed", async ({ page }) => {
  const deleted = (name: string) => ({ action: "delete", name, content: "home.example.com", proxied: true, route: "r" });
  const changes = [deleted("a.example.com"), deleted("b.example.com"), deleted("c.example.com")];
  await mockCloudflarePage(page, { enabled: false }, { "POST /cloudflare/purge": [{ zone: "example.com", changes, errors: ["c.example.com: Cloudflare answered 500"] }] });
  await page.goto("/cloudflare");
  await expect(page.getByRole("region", { name: "Cloudflare sync" })).toContainText("Off");
  await page.getByRole("region", { name: "Remove site records" }).getByRole("button", { name: "Remove site records" }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Remove site records" }).click();
  await expect(page.getByText("Removed 2 site records", { exact: true })).toBeVisible();
  await expect(page.getByRole("region", { name: "Cloudflare sync" })).toContainText("c.example.com: Cloudflare answered 500");
});

test("Adopt on a hand-made site record adopts it, then syncs", async ({ page }) => {
  let adopted = false;
  const zone = () => ({ zone: "example.com", changes: [], errors: [], records: [],
    conflicts: adopted ? [] : [{ name: "app.example.com", route: "app" }] });
  const calls = await mockCloudflarePage(page, {}, {
    "GET /cloudflare/records": () => ({ enabled: true, last_reconcile: null, ddns_checked_at: null, ddns: [], zones: [zone()] }),
    "POST /cloudflare/records/app.example.com/adopt": () => {
      adopted = true;
      return { host: "app.example.com", route: "app" };
    },
    "POST /cloudflare/reconcile": () => ({ at: new Date().toISOString(), error: "", zones: [] }),
    "GET /routes": [{ ...route, cloudflare: { dns: true, proxied: null } }],
  });
  await page.goto("/cloudflare");
  await expect(page.getByRole("listitem").filter({ hasText: "app.example.com" })).toContainText("Made by hand");
  await page.getByRole("button", { name: "Adopt" }).click();
  await expect(page.getByRole("button", { name: "Adopt" })).toHaveCount(0);
  expect(calls["POST /cloudflare/reconcile"]).toBe(1);
  await expect(page).toHaveURL(/\/cloudflare$/); // the row links to the site, and the button must not follow it
});

test("a refused adoption of a site record shows why", async ({ page }) => {
  await mockCloudflarePage(page, {}, {
    "GET /cloudflare/records": () => ({ enabled: true, last_reconcile: null, ddns_checked_at: null, ddns: [],
      zones: [{ zone: "example.com", changes: [], errors: [], records: [], conflicts: [{ name: "app.example.com", route: "app" }] }] }),
  });
  await page.route("**/api/v1/cloudflare/records/*/adopt", (request) => request.fulfill({
    status: 409, contentType: "application/json", body: JSON.stringify({ error: { code: "adopt_refused", message: "app.example.com has 2 records, so there is no one to adopt", field: null } }),
  }));
  await page.goto("/cloudflare");
  await page.getByRole("button", { name: "Adopt" }).click();
  await expect(page.getByText("has 2 records, so there is no one to adopt")).toBeVisible();
});
