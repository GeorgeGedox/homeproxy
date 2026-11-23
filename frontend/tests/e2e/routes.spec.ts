import type { Page } from "@playwright/test";
import { expect, test } from "./fixtures";

// The editor's normal flows run in tests/real. These need a failure or GeoIP data a real, offline stack does not have.
type Route = Record<string, unknown> & { name: string };

interface MockState {
  routes: Route[];
  posted: Route[];
  put: Route[];
  usersBody: unknown;
  /** Refuse the next basic auth save, as a failure after the route was staged. */
  usersFail: boolean;
  /** Refuse to list the basic auth users. */
  usersLoadFail: boolean;
}

/** A stateful mock of the session, routes and basic auth endpoints. */
async function mockApi(page: Page): Promise<MockState> {
  const state: MockState = { routes: [], posted: [], put: [], usersBody: undefined, usersFail: false, usersLoadFail: false };
  const refused = (message: string): [number, unknown] => [500, { error: { code: "internal", message } }];
  const handlers: Record<string, (name: string, body: Route) => [number, unknown?]> = {
    "GET /auth/me": () => [200, { authenticated: true }],
    "GET /changes": () => [200, { changes: [] }],
    "POST /routes/validate": () => [200, { errors: [], warnings: [] }],
    "GET /routes": () => [200, state.routes],
    "GET /geoip": () => [200, { provider: "dbip-lite", block_countries: ["KP"], data: { countries: ["CN", "DE", "KP", "RU"] }, attribution: {} }],
    "GET /routes/warnings": () => [200, []],
    "POST /routes": (_, body) => {
      state.posted.push(body);
      const route = { ...body, name: body.name ?? `new-${state.posted.length}` };
      state.routes.push(route);
      return [201, { route, warnings: [], site_changed: [] }];
    },
    "GET /routes/:name": (name) => {
      const found = state.routes.find((r) => r.name === name);
      return found ? [200, found] : [404, { error: { code: "route_not_found", message: "No such route." } }];
    },
    "PUT /routes/:name": (name, body) => {
      state.put.push(body);
      state.routes = state.routes.map((r) => (r.name === name ? body : r));
      return [200, { route: body, warnings: [], site_changed: [] }];
    },
    "GET /routes/:name/basic-auth": () => (state.usersLoadFail ? refused("The users file could not be read.") : [200, { users: [] }]),
    "PUT /routes/:name/basic-auth": (_, body) => {
      if (state.usersFail) {
        state.usersFail = false;
        return refused("The users were not saved.");
      }
      state.usersBody = body;
      return [200, { users: [] }];
    },
  };
  await page.route("**/api/v1/**", (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname.replace("/api/v1", "");
    const [, , name] = path.split("/");
    const shape = ["validate", "warnings"].includes(name ?? "") ? path : path.replace(`/${name}`, "/:name");
    const [status, data] = handlers[`${request.method()} ${shape}`]?.(name ?? "", request.postDataJSON()) ?? [404, {}];
    return route.fulfill({ status, contentType: "application/json", body: data ? JSON.stringify(data) : "" });
  });
  return state;
}

const wiki: Route = {
  version: 1, name: "wiki", type: "http", hosts: ["wiki.example.com"], exposure: "public",
  upstreams: [{ address: "192.168.1.20:8080" }], enabled: true, websocket: false,
  auth: { type: "none" }, request_headers: {}, response_headers: {}, cloudflare: { dns: false, proxied: null },
  path_prefix: "/", strip_prefix: false,
};
const openWiki = (page: Page) => page.getByRole("region", { name: "wiki.example.com" }).getByRole("link", { name: /192\.168\.1\.20:8080/ }).click();

// A real stack always has sites by the time a test sees Routes: other files add them first.
test("with no sites, Routes says so and offers to add one", async ({ page }) => {
  await mockApi(page);
  await page.goto("/routes");
  await expect(page.getByText("No sites yet")).toBeVisible();
  await page.getByRole("link", { name: "Add site" }).last().click();
  await expect(page).toHaveURL(/\/routes\/edit$/);
});

test("a users save that fails after the site was staged retries as an update", async ({ page }) => {
  const state = await mockApi(page);
  state.usersFail = true;
  await page.goto("/routes/edit");
  await page.getByLabel("Hosts").fill("grafana.example.com");
  await page.getByLabel("Hosts").press("Enter");
  await page.getByLabel("Upstreams").fill("192.168.1.30:3000");
  await page.getByLabel("Upstreams").press("Enter");
  await page.getByRole("switch", { name: /Ask for a password/ }).click();
  await page.getByRole("button", { name: "Add user" }).click();
  await page.getByLabel("User name").fill("alice");
  await page.getByLabel("User password").fill("a-secret");
  await page.getByRole("button", { name: "Stage change" }).click();
  await expect(page.getByText("The users were not saved.")).toBeVisible();

  await page.getByRole("button", { name: "Stage change" }).click();
  await expect(page).toHaveURL(/\/routes$/);
  // A second create would stage the site twice, and the password is never shown: the requests are the outcome.
  expect(state.posted).toHaveLength(1);
  expect(state.put.map((route) => route.name)).toEqual(["new-1"]);
  expect(state.usersBody).toEqual({ users: { alice: "a-secret" } });
});

test("a route whose users cannot be read does not open, so a save cannot delete them", async ({ page }) => {
  const state = await mockApi(page);
  state.routes = [{ ...wiki, auth: { type: "basic" } }];
  state.usersLoadFail = true;
  await page.goto("/routes");
  await openWiki(page);
  await expect(page.getByText("The users file could not be read.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Stage change" })).toHaveCount(0);
});

test("block countries by picking them, and switch the blocking off without losing the list", async ({ page }) => {
  const state = await mockApi(page);
  state.routes = [wiki];
  await page.goto("/routes");
  await openWiki(page);
  await page.getByRole("switch", { name: /Block countries/ }).click();
  await expect(page.getByRole("button", { name: "Block North Korea" })).toBeHidden();
  await expect(page.getByText("Blocked on every public route already: North Korea")).toBeVisible();
  await page.getByLabel("Find a country").fill("rus");
  await page.getByRole("button", { name: "Block Russia" }).click();
  await page.getByLabel("Find a country").fill("");
  await page.getByRole("button", { name: "Block China" }).click();
  await page.getByRole("button", { name: "Unblock Russia" }).click();
  await page.getByRole("button", { name: "Block Germany" }).click();
  await page.getByRole("switch", { name: /Block countries/ }).click();
  await expect(page.getByText("Off. Kept for later: China, Germany")).toBeVisible();
  await page.getByRole("button", { name: "Stage change" }).click();

  await expect(page).toHaveURL(/\/routes$/);
  await openWiki(page);
  await expect(page.getByRole("switch", { name: /Block countries/ })).not.toBeChecked();
  await expect(page.getByText("Off. Kept for later: China, Germany")).toBeVisible();
});
