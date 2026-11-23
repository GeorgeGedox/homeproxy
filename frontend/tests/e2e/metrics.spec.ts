import { expect, test } from "./fixtures";

// A real stack always has its metrics file, so only a mock can refuse it.
test("the metrics page says when the metrics file cannot be opened", async ({ page }) => {
  await page.route("**/api/v1/**", (route) => {
    const path = new URL(route.request().url()).pathname.replace("/api/v1", "");
    if (path === "/auth/me") return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ authenticated: true }) });
    if (path === "/metrics/series") return route.fulfill({ status: 503, contentType: "application/json", body: "{}" });
    return route.fulfill({ status: 404, contentType: "application/json", body: "{}" });
  });
  await page.goto("/metrics");
  await expect(page.getByText("Metrics are unavailable")).toBeVisible();
});
