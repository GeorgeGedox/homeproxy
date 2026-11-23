import type { Page } from "@playwright/test";
import { expect, test } from "./fixtures";

interface MockState {
  setup: boolean;
  authenticated: boolean;
  setupBody: { password: string } | undefined;
  needs: string[];
}

// A real stack is set up once, by the first test that signs in, so its wrong-token and busy-port
// states come from a mock.

/** A stateful mock of the setup and session endpoints. */
async function mockApi(page: Page): Promise<MockState> {
  const state: MockState = { setup: false, authenticated: false, setupBody: undefined, needs: [] };
  const wrongToken = { error: { code: "invalid_setup_token", message: "The setup token is wrong.", field: "token" } };
  const detected = {
    network: { lan_cidrs: ["192.168.1.0/24"], haproxy_lan_ip: "192.168.1.10" },
    dns_server: { listen: "192.168.1.10:5335" },
  };
  type Body = Record<string, string>;
  type Reply = [number, unknown?];
  const handlers: Record<string, (body: Body) => Reply> = {
    "/setup/status": () => [200, state.setup ? { setup_required: false } : { setup_required: true, detected, needs: state.needs }],
    "/auth/me": () => (state.authenticated ? [200, { authenticated: true }] : [401, {}]),
    "/setup": (body) => {
      if (body.token !== "good-token") return [403, wrongToken];
      state.setup = true;
      state.setupBody = body;
      return [204];
    },
    "/auth/login": (body) => {
      state.authenticated = state.setup && body.password === state.setupBody?.password;
      return [state.authenticated ? 204 : 401];
    },
    "/settings": () => [200, { dns_server: detected.dns_server }],
    "/changes": () => [200, { changes: [] }],
  };
  await page.route("**/api/v1/**", (route) => {
    const path = new URL(route.request().url()).pathname.replace("/api/v1", "");
    const [status, data] = handlers[path]?.(route.request().postDataJSON()) ?? [404, {}];
    return route.fulfill({ status, contentType: "application/json", body: data ? JSON.stringify(data) : "" });
  });
  return state;
}

test("setup with the token, log in, then land on Overview", async ({ page }) => {
  const state = await mockApi(page);
  await page.goto("/");
  await expect(page).toHaveURL(/\/setup$/);

  await page.getByLabel("Setup token").fill("wrong-token");
  await page.getByLabel("Admin password").fill("a-long-admin-password");
  await expect(page.getByTestId("setup-summary")).toContainText("LAN ranges192.168.1.0/24");
  await expect(page.getByTestId("setup-summary")).toContainText("Proxy LAN IP192.168.1.10");
  await expect(page.getByTestId("setup-summary")).toContainText("Settings");

  await page.getByLabel("Repeat the password").fill("another-password");
  await expect(page.getByText("The passwords do not match.")).toBeVisible();
  await page.getByRole("button", { name: "Finish setup" }).click();
  expect(state.setupBody).toBeUndefined();

  await page.getByLabel("Repeat the password").fill("a-long-admin-password");
  await page.getByRole("button", { name: "Finish setup" }).click();
  await expect(page.getByText("The setup token is wrong.")).toBeVisible();

  await page.getByLabel("Setup token").fill("good-token");
  await page.getByRole("button", { name: "Finish setup" }).click();
  await expect(page).toHaveURL(/\/$/);
  await expect(page.getByRole("heading", { name: "Overview" })).toBeVisible();
  expect(state.setupBody).toEqual({ token: "good-token", password: "a-long-admin-password" });
});

test("setup asks for the HTTP port another program holds and sends it", async ({ page }) => {
  const state = await mockApi(page);
  state.needs = ["network.http_port"];
  await page.goto("/setup");
  await page.getByLabel("Setup token").fill("good-token");
  await page.getByLabel("Admin password").fill("a-long-admin-password");
  await page.getByLabel("Repeat the password").fill("a-long-admin-password");
  await expect(page.getByText("already listens on port 80")).toBeVisible();
  await page.getByLabel("HTTP port").fill("8081");
  await page.getByRole("button", { name: "Finish setup" }).click();
  await expect(page.getByRole("heading", { name: "Overview" })).toBeVisible();
  expect(state.setupBody).toEqual({
    token: "good-token",
    password: "a-long-admin-password",
    network: { http_port: 8081 },
  });
});
