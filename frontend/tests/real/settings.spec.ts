import { apply, expect, signIn, test } from "./fixtures";

test.describe.configure({ mode: "serial" });

test("staged settings wait for Stage changes and the apply, and a refused value shows at its field", async ({ page, stack }) => {
  await signIn(page, stack);
  await page.goto("/settings");
  const stage = page.getByRole("button", { name: "Stage changes" });
  await expect(stage).toBeDisabled();

  await page.getByRole("navigation", { name: "Settings sections" }).getByRole("link", { name: "DNS server" }).click();
  const dns = page.getByRole("region", { name: "DNS server" });
  const upstreams = dns.getByLabel("DNS upstreams");
  await upstreams.fill("9.9.9.9");
  await upstreams.press("Enter");
  await page.getByRole("navigation", { name: "Settings sections" }).getByRole("link", { name: "Certificates" }).click();
  await page.getByRole("switch", { name: "Use Let's Encrypt staging" }).click();
  await expect(page.getByText("Changed: DNS server, Certificates")).toBeVisible();

  // A sync interval under the bound is refused at its field, from another tab too, and nothing is staged.
  await page.getByRole("navigation", { name: "Settings sections" }).getByRole("link", { name: "Cloudflare" }).click();
  await page.getByRole("switch", { name: "Keep Cloudflare DNS records" }).click();
  await page.getByLabel("Sync interval").fill("1m");
  await page.getByRole("navigation", { name: "Settings sections" }).getByRole("link", { name: "Network" }).click();
  await stage.click();
  await expect(page.getByLabel("Sync interval")).toBeVisible();
  await expect(page.getByRole("region", { name: "Cloudflare" })).toContainText("5m");
  await expect(page.getByTestId("pending-bar")).toHaveCount(0);

  await page.getByLabel("Sync interval").fill("15m");
  await page.getByRole("switch", { name: "Keep Cloudflare DNS records" }).click();
  await stage.click();
  const bar = page.getByTestId("pending-bar");
  await expect(bar).toContainText("2 changes staged");
  await expect(stage).toBeDisabled();
  await apply(page);
  await page.goto("/dns");
  await expect(page.getByRole("region", { name: "DNS server" })).toContainText("9.9.9.9");

  // Leave the shared stack as it was.
  await page.goto("/settings");
  await page.getByRole("navigation", { name: "Settings sections" }).getByRole("link", { name: "DNS server" }).click();
  await dns.getByRole("button", { name: "9.9.9.9" }).click();
  await page.getByRole("navigation", { name: "Settings sections" }).getByRole("link", { name: "Certificates" }).click();
  await page.getByRole("switch", { name: "Use Let's Encrypt staging" }).click();
  await stage.click();
  await apply(page);
});

test("discard asks first, names what goes, and Settings shows the saved values again", async ({ page, stack }) => {
  await signIn(page, stack);
  await page.goto("/settings");
  await page.getByRole("navigation", { name: "Settings sections" }).getByRole("link", { name: "DNS server" }).click();
  const dns = page.getByRole("region", { name: "DNS server" });
  await dns.getByLabel("DNS upstreams").fill("9.9.9.9");
  await dns.getByLabel("DNS upstreams").press("Enter");
  const stage = page.getByRole("button", { name: "Stage changes" });
  await stage.click();
  const bar = page.getByTestId("pending-bar");
  await expect(bar).toContainText("DNS server settings changed");
  await expect(dns.getByRole("button", { name: "9.9.9.9" })).toBeVisible();

  await bar.getByRole("button", { name: "Discard" }).click();
  const confirm = page.getByRole("dialog");
  await expect(confirm).toContainText("Discard 1 staged change?");
  await expect(confirm).toContainText("DNS server settings changed");
  await confirm.getByRole("button", { name: "Discard changes" }).click();
  await expect(bar).toHaveCount(0);
  // The form reloads, or its next save would stage the discarded value again.
  await expect(dns.getByRole("button", { name: "9.9.9.9" })).toHaveCount(0);
  await expect(stage).toBeDisabled();
});

test("the session length is saved at once, with nothing staged", async ({ page, stack }) => {
  await signIn(page, stack);
  await page.goto("/settings/dashboard");
  const length = page.getByLabel("Session length");
  const before = await length.inputValue();
  await length.fill("13h");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.getByText("Saved.")).toBeVisible();
  await expect(page.getByTestId("pending-bar")).toHaveCount(0);
  await page.reload();
  await expect(length).toHaveValue("13h");
  await length.fill(before);
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.getByText("Saved.")).toBeVisible();
});
