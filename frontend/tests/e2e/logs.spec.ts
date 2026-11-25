import { expect, routeApi, test } from "./fixtures";

const entry = (client_ip: string, country: string, request: string) => ({
  kind: "http", logged_at: "2026-11-09T17:08:10.587Z", client_ip, frontend: "fe_https", status: 200,
  request: `GET ${request} HTTP/1.1`, sni: null, route: null, country, line: request,
});
const LAN_LINE = entry("192.168.1.10", "LAN", "/from-lan");
const INTERNET_LINE = entry("203.0.113.9", "DE", "/from-internet");

// The real stack has only LAN clients, so only a mock can show an internet client and the chart's LAN row.
test("the From filter changes the lines shown in the requests tail", async ({ page }) => {
  const asked: string[] = [];
  await routeApi(page, (_method, path) => {
    if (path === "/auth/me") return [200, { authenticated: true }];
    if (path === "/logs/files") return [200, [{ source: "access", size: 0, modified: null }]];
    if (path === "/routes") return [200, []];
  });
  // routeApi answers JSON only, and the stream is server-sent events. This route is added last, so it wins.
  await page.route("**/api/v1/logs/stream*", (route) => {
    const network = new URL(route.request().url()).searchParams.get("network") ?? "";
    asked.push(network);
    const lines = [LAN_LINE, INTERNET_LINE].filter((line) => !network || (network === "lan") === (line.country === "LAN"));
    return route.fulfill({ status: 200, contentType: "text/event-stream", body: lines.map((line) => `data: ${JSON.stringify(line)}\n\n`).join("") });
  });
  await page.goto("/logs");
  await expect(page.getByText("GET /from-lan")).toBeVisible();
  await expect(page.getByText("GET /from-internet")).toBeVisible();

  await page.getByLabel("From").click();
  await page.getByRole("option", { name: "Internet" }).click();
  await expect(page.getByText("GET /from-internet")).toBeVisible();
  await expect(page.getByText("GET /from-lan")).toBeHidden();
  expect(asked).toContain("internet");
});

test("the countries chart lists LAN apart from Unknown", async ({ page }) => {
  await routeApi(page, (_method, path) => {
    if (path === "/auth/me") return [200, { authenticated: true }];
    if (path === "/routes") return [200, []];
    if (path !== "/metrics/series") return;
    // The query is not passed to the reply, so one answer holds every group: the total and the countries.
    const at = new Date(Date.now() - 5 * 60_000).toISOString();
    return [200, [["LAN", 7], [null, 2], ["DE", 1]].map(([group, requests]) => ({ at, group, requests }))];
  });
  await page.goto("/metrics");
  const countries = page.getByRole("region", { name: "Countries" });
  await expect(countries.getByRole("button", { name: "Show LAN over time" })).toBeVisible();
  await expect(countries.getByRole("button", { name: "Show Unknown over time" })).toBeVisible();
});
