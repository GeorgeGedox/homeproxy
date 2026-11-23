import { addDomain, addSite, apply, expect, signIn, test } from "./fixtures";
import { viaHaproxy } from "./stack";

const SITE = "http://app.example.info";

test("requests through HAProxy show in the total, by site and by status, and one site can be shown over time", async ({ page, stack }) => {
  await signIn(page, stack);
  await addDomain(page, "example.info");
  await addSite(page, stack, "app.example.info");
  await apply(page);
  for (let i = 0; i < 3; i += 1) expect((await viaHaproxy(stack, `${SITE}/`)).status).toBe(200);

  await page.goto("/metrics");
  const sites = page.getByRole("region", { name: "Sites" });
  const site = sites.getByRole("button", { name: "Show app.example.info over time" });
  // The dashboard counts the access lines as HAProxy sends them, so give it a moment.
  await expect(async () => {
    await page.reload();
    await expect(site).toBeVisible({ timeout: 1000 });
  }).toPass({ timeout: 30_000 });
  await expect(page.getByRole("region", { name: "Status codes" }).getByRole("button", { name: "Show 200 over time" })).toBeVisible();
  await expect(page.getByRole("region", { name: "Countries" })).toContainText("Unknown");

  await site.click();
  await expect(page.getByRole("heading", { name: "Requests to app.example.info" })).toBeVisible();
  // Longer ranges read the hourly table, which holds the requests of the current hour too.
  await page.getByRole("tab", { name: "7d" }).click();
  await expect(page.getByText("Requests per hour")).toBeVisible();
  await expect(site).toContainText("3");
  await page.getByRole("button", { name: "Show all" }).click();
  await expect(page.getByRole("heading", { name: "Requests", exact: true })).toBeVisible();
});
