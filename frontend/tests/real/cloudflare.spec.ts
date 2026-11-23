import { apply, expect, signIn, test } from "./fixtures";

test.describe.configure({ mode: "serial" });

test("while Cloudflare is off, the page says so and points to Settings", async ({ page, stack }) => {
  await signIn(page, stack);
  await page.goto("/cloudflare");
  const sync = page.getByRole("region", { name: "Cloudflare sync" });
  await expect(sync).toContainText("Off");
  await expect(sync).toContainText("leaves your Cloudflare DNS records as they are");
  // Nothing to remove without a Cloudflare domain, and nothing to sync while off.
  await expect(page.getByRole("region", { name: "Remove site records" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Sync now" })).toHaveCount(0);
  await sync.getByRole("link", { name: "Turn it on in Settings" }).click();
  await expect(page).toHaveURL(/\/settings#cloudflare$/);
  await expect(page.getByRole("switch", { name: "Keep Cloudflare DNS records" })).toBeVisible();
});

test("turned on with no domain on Cloudflare, the page points to Domains", async ({ page, stack }) => {
  await signIn(page, stack);
  await page.goto("/settings");
  await page.getByRole("navigation", { name: "Settings sections" }).getByRole("link", { name: "Cloudflare" }).click();
  const cloudflare = page.getByRole("switch", { name: "Keep Cloudflare DNS records" });
  await cloudflare.click();
  await page.getByRole("button", { name: "Stage changes" }).click();
  await apply(page);

  await page.goto("/cloudflare");
  await expect(page.getByRole("region", { name: "Cloudflare sync" })).toContainText("No domain on Cloudflare");
  await expect(page.getByText("No domain has its DNS on Cloudflare")).toBeVisible();
  await expect(page.getByRole("button", { name: "Sync now" })).toHaveCount(0);
  await page.getByRole("link", { name: "Open Domains" }).click();
  await expect(page).toHaveURL(/\/domains$/);

  // Leave the shared stack as it was found.
  await page.goto("/settings");
  await page.getByRole("navigation", { name: "Settings sections" }).getByRole("link", { name: "Cloudflare" }).click();
  await cloudflare.click();
  await page.getByRole("button", { name: "Stage changes" }).click();
  await apply(page);
});
