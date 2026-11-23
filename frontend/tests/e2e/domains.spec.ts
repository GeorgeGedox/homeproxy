import { expect, test } from "./fixtures";

// The domain flows run in tests/real; a real stack does not refuse to list its credentials.
test("a domain cannot be staged while the stored credentials fail to load, so it never names new ones over them", async ({ page }) => {
  await page.route("**/api/v1/**", (route) => {
    const path = new URL(route.request().url()).pathname.replace("/api/v1", "");
    const reply = (status: number, body: unknown) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (path === "/auth/me") return reply(200, { authenticated: true });
    if (path === "/domains/providers") return reply(200, []);
    if (path === "/credentials") return reply(500, { error: { code: "internal", message: "The secrets file could not be read." } });
    return reply(404, {});
  });
  await page.goto("/domains/edit");
  await page.getByLabel("Domain", { exact: true }).fill("example.org");
  await expect(page.getByText("The secrets file could not be read.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Stage change" })).toBeDisabled();
});
