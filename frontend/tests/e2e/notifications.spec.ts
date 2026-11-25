import type { Page } from "@playwright/test";
import { expect, routeApi, test, type Reply } from "./fixtures";

const DISCORD_URL = "https://discord.com/api/webhooks/123/discord-secret-token";
const HEARTBEAT_URL = "https://hc-ping.example.com/heartbeat-secret-uuid";
const SECRETS = [DISCORD_URL, HEARTBEAT_URL, "discord-secret-token", "heartbeat-secret-uuid"];

type Body = Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any

interface Mock {
  settingsBody: Body | undefined;
  urlBodies: unknown[];
}

/**
 * A mock of settings and notification URLs. The test buttons need a receiver on the internet, so this
 * flow runs against a mock; what the page sends is the outcome, so the test reads the request bodies.
 */
async function mockApi(page: Page): Promise<Mock> {
  const mock: Mock = { settingsBody: undefined, urlBodies: [] };
  const urls = { targets: {} as Record<string, boolean>, unused: ["old-name"], heartbeat_url: false };
  const settings = {
    network: { lan_cidrs: ["192.168.1.0/24"], haproxy_lan_ip: "192.168.1.10", http_port: 80, https_port: 443, public_ip: "auto", ipv6: false },
    dns_server: { listen: "192.168.1.10:5335", forward_unknown_to: [] },
    cloudflare: { enabled: false },
    geoip: { provider: "dbip-lite", block_countries: [] },
    crowdsec: { enabled: false, captcha_provider: "none", allowlist: [], appsec: false },
    prometheus: { enabled: false },
    notifications: {},
  };
  const handlers: Record<string, (body: Body) => Reply> = {
    "GET /auth/me": () => [200, { authenticated: true }],
    "GET /settings": () => [200, settings],
    "PUT /settings": (body) => {
      mock.settingsBody = body;
      for (const target of body.notifications?.targets ?? []) urls.targets[target.name] ??= false;
      return [200, { changed: true, warnings: [] }];
    },
    "GET /settings/keys": () => [200, { crowdsec: {}, geoip: {} }],
    "GET /settings/dashboard": () => [200, { listen: "192.168.1.10:8080", session_ttl: "12h", pending_listen: null }],
    "GET /notifications/urls": () => [200, urls],
    "PUT /notifications/urls": (body) => {
      mock.urlBodies.push(body);
      for (const [name, value] of Object.entries<string>(body.targets ?? {})) {
        if (value === "") urls.unused = urls.unused.filter((unused) => unused !== name);
        else urls.targets[name] = true;
      }
      if (body.heartbeat_url !== undefined) urls.heartbeat_url = body.heartbeat_url !== "";
      return [200, urls];
    },
    "POST /notifications/targets/alerts/test": () => [200, { status: 204, error: null }],
    "POST /notifications/heartbeat/test": () => [200, { status: 200, error: null }],
    "GET /status": () => [200, { haproxy_version: "3.4.0", haproxy_error: null, version_check: null, last_apply: null, routes: [], bouncer: null }],
    "GET /changes": () => [200, { changes: [], conflict: false, conflicting_paths: [], config_diff: "", generated_diff: "", generated_error: null }],
  };
  await routeApi<Body | null>(page, (method, path, body) => handlers[`${method} ${path}`]?.(body ?? {}));
  return mock;
}

test("a Discord target gets a test message, the heartbeat gets a URL, and no URL is ever shown", async ({ page }) => {
  const mock = await mockApi(page);
  await page.goto("/settings");
  await page.getByRole("navigation", { name: "Settings sections" }).getByRole("link", { name: "Notifications" }).click();

  await page.getByRole("button", { name: "Add target" }).click();
  await page.getByLabel("Target name").fill("alerts");
  await expect(page.getByText("Save the settings to set this target's URL.")).toBeVisible();
  await page.getByLabel("Heartbeat", { exact: true }).click();
  await page.getByLabel("Heartbeat interval").fill("30s");
  await page.getByRole("button", { name: "Stage changes" }).click();
  await expect(page.getByText("No changes")).toBeVisible();
  expect(mock.settingsBody?.notifications).toMatchObject({
    targets: [{ name: "alerts", type: "discord" }],
    heartbeat: { enabled: true, interval: "30s" },
  });

  await expect(page.getByRole("button", { name: "Test alerts" })).toBeDisabled();
  await page.getByRole("button", { name: "Add URL of alerts" }).click();
  await page.getByLabel("New URL of alerts").fill(DISCORD_URL);
  await page.getByRole("button", { name: "Save URL of alerts" }).click();
  await expect(page.getByTestId("url-URL of alerts")).toHaveText("Set");
  await page.getByRole("button", { name: "Test alerts" }).click();
  await expect(page.getByText("The receiver answered 204.")).toBeVisible();

  await page.getByRole("button", { name: "Add Heartbeat URL" }).click();
  await page.getByLabel("New Heartbeat URL").fill(HEARTBEAT_URL);
  await page.getByRole("button", { name: "Save Heartbeat URL" }).click();
  await page.getByRole("button", { name: "Test heartbeat" }).click();
  await expect(page.getByText("The receiver answered 200.")).toBeVisible();

  await page.getByRole("button", { name: "Clear leftover URL of old-name" }).click();
  await expect(page.getByText("A URL is left over for old-name")).toBeHidden();
  expect(mock.urlBodies).toContainEqual({ targets: { "old-name": "" } });

  const shown = await page.content();
  for (const secret of SECRETS) expect(shown).not.toContain(secret);
});
