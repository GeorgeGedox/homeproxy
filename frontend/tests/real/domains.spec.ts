import { addSite, apply, expect, signIn, test } from "./fixtures";
import { servedCertificate } from "./stack";

test.describe.configure({ mode: "serial" });

test("a self-signed domain gets its certificate at the apply, and HAProxy serves it", async ({ page, stack }) => {
  await signIn(page, stack);
  await page.goto("/domains");
  await page.getByRole("link", { name: "Add domain" }).first().click();
  await page.getByLabel("Domain", { exact: true }).fill("example.net");
  await page.getByRole("radio", { name: /Self-signed/ }).click();
  await expect(page.getByRole("switch", { name: "One wildcard certificate for *.example.net" })).toBeChecked();
  await page.getByRole("button", { name: "Stage change" }).click();

  const card = page.getByRole("region", { name: "example.net" });
  await expect(card.getByText("staged")).toBeVisible();
  await expect(card.getByText("Issued after the next apply")).toBeVisible();
  await apply(page);
  const certificate = card.getByRole("list", { name: "Certificates of example.net" }).getByRole("listitem");
  await expect(certificate).toContainText("example.net, *.example.net", { timeout: 30_000 });
  await expect(certificate).toContainText("days left");
  const served = await servedCertificate(stack, "app.example.net");
  expect(served.names).toContain("DNS:*.example.net");

  // Renew makes a new certificate, and HAProxy serves it without an apply.
  await certificate.getByRole("button", { name: "Renew example.net" }).click();
  await expect.poll(async () => (await servedCertificate(stack, "app.example.net")).fingerprint, { timeout: 30_000 }).not.toBe(served.fingerprint);
  await expect(certificate).toContainText("days left");
});

test("a Route 53 domain's keys show on the credentials card only as set, and Replace keeps the region", async ({ page, stack }) => {
  await signIn(page, stack);
  await page.goto("/domains");
  await page.getByRole("link", { name: "Add domain" }).first().click();
  await page.getByLabel("Domain", { exact: true }).fill("example.org");
  await page.getByRole("radio", { name: /another DNS provider/ }).click();
  await page.getByText("Find your provider").click();
  await page.keyboard.type("Route 53");
  await page.getByRole("option", { name: "Amazon Route 53" }).click();
  await page.getByLabel("AWS_ACCESS_KEY_ID").fill("AKIAEXAMPLEKEY");
  await page.getByLabel("AWS_SECRET_ACCESS_KEY").fill("an-example-secret-key");
  await page.getByRole("button", { name: "More options" }).click();
  await page.getByLabel("AWS_REGION").fill("eu-west-1");
  await page.getByRole("button", { name: "Stage change" }).click();
  await expect(page).toHaveURL(/\/domains$/);

  const card = page.getByRole("region", { name: "DNS credentials" });
  const row = card.getByRole("listitem").filter({ hasText: "Amazon Route 53" });
  await expect(row).toContainText("AWS_SECRET_ACCESS_KEY set");
  await expect(row).toContainText("AWS_REGION eu-west-1");
  await expect(row).toContainText("Used by example.org");
  await expect(card.getByRole("button", { name: /^Delete / })).toHaveCount(0);
  await expect(page.getByText("an-example-secret-key")).toHaveCount(0);
  await expect(page.getByText("AKIAEXAMPLEKEY")).toHaveCount(0);

  // Replace one secret: the others and the region stay, and nothing secret comes back.
  await row.getByRole("button", { name: /^Replace / }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByLabel("AWS_REGION")).toHaveValue("eu-west-1");
  await dialog.getByLabel("AWS_SECRET_ACCESS_KEY").fill("a-replaced-secret-key");
  await dialog.getByRole("button", { name: "Save" }).click();
  await expect(dialog).toHaveCount(0);
  await expect(row).toContainText("AWS_REGION eu-west-1");
  await expect(row).toContainText("AWS_ACCESS_KEY_ID set");
  await expect(page.getByText("a-replaced-secret-key")).toHaveCount(0);
});

test("a domain with sites cannot be removed until they go, and one without is removed at the apply", async ({ page, stack }) => {
  await signIn(page, stack);
  await addSite(page, stack, "app.example.net");
  await apply(page);

  await page.goto("/domains");
  const remove = async (zone: string) => {
    await page.getByRole("button", { name: `Actions for ${zone}` }).click();
    await page.getByRole("menuitem", { name: "Remove" }).click();
  };
  await remove("example.net");
  await expect(page.getByRole("dialog")).toContainText("One site uses it");
  await expect(page.getByRole("button", { name: "Remove domain" })).toHaveCount(0);
  await page.getByRole("button", { name: "Keep it" }).click();

  await remove("example.org");
  await page.getByRole("button", { name: "Remove domain" }).click();
  await apply(page);
  await expect(page.getByRole("region", { name: "example.org" })).toHaveCount(0);

  // Its Route 53 keys are unused now, so they can be deleted.
  const row = page.getByRole("region", { name: "DNS credentials" }).getByRole("listitem").filter({ hasText: "Amazon Route 53" });
  await expect(row).toContainText("Not used");
  await row.getByRole("button", { name: /^Delete / }).click();
  await page.getByRole("button", { name: "Delete credentials" }).click();
  await expect(row).toHaveCount(0);
});
