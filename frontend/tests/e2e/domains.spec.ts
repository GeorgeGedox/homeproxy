import { expect, routeApi, test } from "./fixtures";

// The domain flows run in tests/real; a real stack does not refuse to list its credentials.
test("a domain cannot be staged while the stored credentials fail to load, so it never names new ones over them", async ({ page }) => {
  await routeApi(page, (_, path) => {
    if (path === "/auth/me") return [200, { authenticated: true }];
    if (path === "/domains/providers") return [200, []];
    if (path === "/credentials") return [500, { error: { code: "internal", message: "The secrets file could not be read." } }];
    return undefined;
  });
  await page.goto("/domains/edit");
  await page.getByLabel("Domain", { exact: true }).fill("example.org");
  await expect(page.getByText("The secrets file could not be read.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Stage change" })).toBeDisabled();
});
