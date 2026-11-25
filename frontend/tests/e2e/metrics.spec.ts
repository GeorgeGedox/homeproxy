import { expect, routeApi, test } from "./fixtures";

// A real stack always has its metrics file, so only a mock can refuse it.
test("the metrics page says when the metrics file cannot be opened", async ({ page }) => {
  await routeApi(page, (_, path) => {
    if (path === "/auth/me") return [200, { authenticated: true }];
    if (path === "/metrics/series") return [503, {}];
    return undefined;
  });
  await page.goto("/metrics");
  await expect(page.getByText("Metrics are unavailable")).toBeVisible();
});
