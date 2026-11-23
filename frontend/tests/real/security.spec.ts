import { expect, logIn, signIn, test } from "./fixtures";

test.describe.configure({ mode: "serial" });

test("with CrowdSec off, the page points to Settings and Ban an address is disabled", async ({ page, stack }) => {
  await signIn(page, stack);
  await page.goto("/security");
  const crowdsec = page.getByRole("region", { name: "CrowdSec" });
  await expect(crowdsec).toContainText("Off");
  await expect(crowdsec.getByRole("link", { name: "in Settings" })).toHaveAttribute("href", "/settings#crowdsec");
  await expect(page.getByRole("button", { name: "Ban an address" })).toBeDisabled();
});

test("an allowlist entry takes effect at once, a public range warns, and both can be removed", async ({ page, stack }) => {
  await signIn(page, stack);
  await page.goto("/security");
  const allowed = page.getByRole("region", { name: "Always allowed" });
  const field = allowed.getByLabel("Address or range to allow");
  await field.fill("192.168.50.0/24");
  await allowed.getByRole("button", { name: "Allow" }).click();
  await expect(allowed.getByRole("listitem")).toHaveText(["192.168.50.0/24"]);
  await expect(allowed).not.toContainText("public");

  await field.fill("198.51.0.0/16");
  await allowed.getByRole("button", { name: "Allow" }).click();
  await expect(allowed).toContainText("198.51.0.0/16");
  await expect(allowed.getByText(/198\.51\.0\.0\/16.*(outside|public)/)).toBeVisible();
  // Nothing is staged: the allowlist is saved and live at once.
  await expect(page.getByTestId("pending-bar")).toHaveCount(0);

  await page.reload();
  await expect(allowed.getByRole("listitem")).toHaveCount(2);
  await allowed.getByRole("button", { name: "Remove 198.51.0.0/16" }).click();
  await allowed.getByRole("button", { name: "Remove 192.168.50.0/24" }).click();
  await expect(allowed.getByRole("listitem")).toHaveCount(0);
});

test("changes saved at once, such as the allowlist edits above, show on History's Activity tab", async ({ page }) => {
  await logIn(page);
  await page.goto("/history");
  await page.getByRole("tab", { name: "Activity" }).click();
  const activity = page.getByRole("region", { name: "Activity" });
  await expect(activity).toContainText("192.168.50.0/24 always allowed");
  await expect(activity).toContainText("192.168.50.0/24 no longer always allowed");
});

test("before the first GeoIP download, Countries says so and credits the provider", async ({ page, stack }) => {
  await signIn(page, stack);
  await page.goto("/security");
  const countries = page.getByRole("region", { name: "Countries" });
  // The stack is offline, so its scheduled download may have failed by now: the light can say either.
  await expect(countries).toContainText("Download the data first");
  await expect(countries.getByRole("link", { name: /DB-IP/ })).toHaveAttribute("href", /db-ip\.com/);
});
