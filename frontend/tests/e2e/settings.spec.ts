import type { Page } from "@playwright/test";
import { expect, routeApi, test, type Reply } from "./fixtures";

const SECRET = "s3cret-value";
const CHECKS = [
  { part: "machine", ok: true, message: "The machine logs in to the LAPI." },
  { part: "bouncer", ok: false, message: "The LAPI refuses the bouncer key." },
  { part: "appsec", ok: null, message: "AppSec is off." },
  { part: "blocklist", ok: true, message: "0 blocklist decisions are active." },
];

interface Mock {
  settingsBody: Record<string, Record<string, unknown>> | undefined;
  keysBodies: unknown[];
  dashboardBodies: unknown[];
  keysSet: Record<string, Record<string, boolean>>;
  bouncer: "up" | "down" | null;
  refuseListen: boolean;
}

const PUBLIC_RANGE_WARNING = "network.lan_cidrs: 0.0.0.0/1 is a public range, so its clients count as LAN";

// CrowdSec's section, keys and bouncer state need a LAPI a real stack lacks, and the dashboard address
// revert needs a clock. The staged-settings flow without CrowdSec runs in tests/real.

/** A mock of settings, live dashboard settings, write-only keys, status and changes. */
async function mockApi(page: Page): Promise<Mock> {
  const mock: Mock = {
    settingsBody: undefined,
    keysBodies: [],
    dashboardBodies: [],
    keysSet: { crowdsec: { bouncer_key: true, machine_id: false, machine_password: false, captcha_site_key: false, captcha_secret_key: false, captcha_signing_key: false }, geoip: { maxmind_license_key: false } },
    bouncer: "down",
    refuseListen: false,
  };
  const settings = {
    network: { lan_cidrs: ["192.168.1.0/24"], haproxy_lan_ip: "192.168.1.10", http_port: 80, https_port: 443, public_ip: "auto", ipv6: false },
    dns_server: { listen: "192.168.1.10:5335", forward_unknown_to: [] },
    cloudflare: { enabled: false },
    geoip: { provider: "maxmind", block_countries: [] },
    crowdsec: { enabled: true, captcha_provider: "none", allowlist: [], appsec: false, lapi_url: "http://127.0.0.1:8080" },
    prometheus: { enabled: false },
    notifications: {},
  };
  const handlers: Record<string, (body: Record<string, Record<string, string>>) => Reply> = {
    "GET /auth/me": () => [200, { authenticated: true }],
    "GET /settings": () => [200, settings],
    "PUT /settings": (body) => {
      mock.settingsBody = body;
      return [200, { changed: true, warnings: [PUBLIC_RANGE_WARNING] }];
    },
    "GET /settings/keys": () => [200, mock.keysSet],
    "PUT /settings/keys": (body) => {
      mock.keysBodies.push(body);
      for (const [section, keys] of Object.entries(body)) {
        for (const [name, value] of Object.entries(keys)) mock.keysSet[section] = { ...mock.keysSet[section], [name]: value !== "" };
      }
      return [200, mock.keysSet];
    },
    "GET /settings/dashboard": () => [200, { listen: "192.168.1.10:8080", session_ttl: "12h", pending_listen: null }],
    "PUT /settings/dashboard": (body) => {
      mock.dashboardBodies.push(body);
      if (mock.refuseListen) return [422, { error: { code: "listen_refused", message: "Port 443 is used by HAProxy.", field: "listen" } }];
      const expires = new Date(Date.now() + 60_000).toISOString();
      return [200, { listen: "192.168.1.10:8080", session_ttl: "24h", pending_listen: { listen: "192.168.1.20:8080", url: "https://192.168.1.20:8080/", expires_at: expires } }];
    },
    "GET /status": () => [200, { haproxy_version: "3.4.0", haproxy_error: null, version_check: null, last_apply: null, routes: [], bouncer: mock.bouncer, appsec: "down" }],
    "POST /security/crowdsec/check": () => [200, CHECKS],
    "GET /changes": () => [200, { changes: [], conflict: false, conflicting_paths: [], config_diff: "", generated_diff: "", generated_error: null }],
  };
  await routeApi<Record<string, Record<string, string>> | null>(page, (method, path, body) => handlers[`${method} ${path}`]?.(body ?? {}));
  return mock;
}

test("each staged setting saves in one request, its warnings show, and the bouncer state shows", async ({ page }) => {
  const mock = await mockApi(page);
  await page.goto("/settings");
  await expect(page.getByTestId("bouncer-state")).toHaveText("Bouncer down");

  await page.getByLabel("Public IP").fill("203.0.113.5");
  await page.getByRole("navigation", { name: "Settings sections" }).getByRole("link", { name: "CrowdSec" }).click();
  await page.getByLabel("Captcha provider").click();
  await page.getByRole("option", { name: "turnstile" }).click();
  await page.getByLabel("AppSec (WAF)").click();
  await expect(page.getByText("trips two different AppSec rules")).toBeVisible();
  await page.getByLabel("AppSec URL").fill("http://192.168.1.10:7422");
  await page.getByRole("navigation", { name: "Settings sections" }).getByRole("link", { name: "Prometheus" }).click();
  await page.getByLabel("Prometheus exporter").click();
  await page.getByLabel("Prometheus listen address").fill("192.168.1.10:9101");
  await page.getByRole("button", { name: "Stage changes" }).click();

  await expect.poll(() => mock.settingsBody).toMatchObject({
    network: { public_ip: "203.0.113.5" },
    crowdsec: { captcha_provider: "turnstile", appsec: true, appsec_url: "http://192.168.1.10:7422" },
    prometheus: { enabled: true, listen: "192.168.1.10:9101" },
  });
  await expect(page.getByText(PUBLIC_RANGE_WARNING)).toBeVisible();
});

// The real stack has no CrowdSec engine, so the AppSec state and the connection test come from the mock.
test("AppSec down shows next to the bouncer, and the connection test shows one line for each part", async ({ page }) => {
  await mockApi(page);
  await page.goto("/settings#crowdsec");
  await expect(page.getByText("AppSec down", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Test connection" }).click();
  await expect(page.getByRole("list", { name: "Connection test" }).getByRole("listitem")).toHaveText(CHECKS.map((check) => check.message));
});

test("keys show set or not set, never a value, and clearing a needed key asks first", async ({ page }) => {
  const mock = await mockApi(page);
  await page.goto("/settings");
  await page.getByRole("navigation", { name: "Settings sections" }).getByRole("link", { name: "CrowdSec" }).click();
  const badge = (name: string) => page.getByTestId(`key-${name}`);
  await expect(badge("bouncer_key")).toHaveText("Set");
  await expect(badge("machine_id")).toHaveText("Not set");

  await page.getByRole("button", { name: "Add Machine ID" }).click();
  await expect(page.getByLabel("New Machine ID")).toHaveValue("");
  await page.getByLabel("New Machine ID").fill(SECRET);
  await page.getByRole("button", { name: "Save Machine ID" }).click();
  await expect.poll(() => mock.keysBodies).toEqual([{ crowdsec: { machine_id: SECRET } }]);
  await expect(page.getByText(SECRET)).toHaveCount(0);
  await expect(badge("machine_id")).toHaveText("Set");

  await page.getByRole("button", { name: "Clear Bouncer key" }).click();
  await expect(page.getByText("The bouncer cannot read decisions.")).toBeVisible();
  await page.getByRole("button", { name: "Keep the key" }).click();
  expect(mock.keysBodies).toHaveLength(1);
  await page.getByRole("button", { name: "Clear Bouncer key" }).click();
  await page.getByRole("button", { name: "Clear the key" }).click();
  await expect.poll(() => mock.keysBodies[1]).toEqual({ crowdsec: { bouncer_key: "" } });
});

test("a dashboard address change is explained first, shows the link and countdown, then says it reverted", async ({ page }) => {
  const mock = await mockApi(page);
  await page.clock.install();
  await page.goto("/settings/dashboard");
  await expect(page.getByLabel("Session length")).toHaveValue("12h");
  await page.getByLabel("Dashboard address").fill("192.168.1.20:8080");
  await expect(page.getByText("sign in within 60 seconds")).toBeVisible();

  mock.refuseListen = true;
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.getByText("Port 443 is used by HAProxy.")).toBeVisible();

  mock.refuseListen = false;
  await page.getByLabel("Session length").fill("24h");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect.poll(() => mock.dashboardBodies.at(-1)).toEqual({ listen: "192.168.1.20:8080", session_ttl: "24h" });
  const pending = page.getByTestId("listen-pending");
  await expect(pending).toContainText(/within \d+ s/);
  await expect(pending.getByRole("link", { name: "Open the new address" })).toHaveAttribute("href", "https://192.168.1.20:8080/");

  await page.clock.fastForward(61_000);
  await expect(page.getByTestId("listen-reverted")).toContainText("192.168.1.20:8080");
  await expect(page.getByText("Saved.", { exact: true })).toBeHidden();
});
