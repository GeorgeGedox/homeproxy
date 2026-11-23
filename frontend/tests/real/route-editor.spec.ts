import { expect, signIn, test } from "./fixtures";

// The editor alone: these tests stage nothing, so they leave the shared stack as they found it.
test("fields hide by route type, and only a website has Advanced", async ({ page, stack }) => {
  await signIn(page, stack);
  await page.goto("/routes");
  await page.getByRole("link", { name: "Add site" }).first().click();
  await page.getByRole("button", { name: "Advanced" }).click();
  const dnsAnswer = page.getByText("What the internal DNS server answers for", { exact: false });
  for (const label of ["Hosts", "Path", "Request headers", "WebSocket", "Health check path"]) {
    await expect(page.getByText(label, { exact: true })).toBeVisible();
  }
  await expect(dnsAnswer).toBeVisible();
  await expect(page.getByRole("switch", { name: /Ask for a password/ })).toBeVisible();
  await expect(page.getByText("Port", { exact: true })).toBeHidden();

  await page.getByRole("radio", { name: /TLS passthrough/ }).click();
  await expect(dnsAnswer).toBeVisible();
  for (const label of ["Path", "Health check path"]) {
    await expect(page.getByText(label, { exact: true })).toBeHidden();
  }
  await expect(page.getByRole("button", { name: "Advanced" })).toBeHidden();
  await expect(page.getByRole("switch", { name: /Ask for a password/ })).toBeHidden();

  await page.getByRole("radio", { name: /TCP port/ }).click();
  await expect(page.getByText("Port", { exact: true })).toBeVisible();
  await expect(page.getByText("Hosts", { exact: true })).toBeHidden();
  await expect(dnsAnswer).toBeHidden();
  await expect(page.getByRole("button", { name: "Advanced" })).toBeHidden();
  await expect(page.getByRole("switch", { name: /Block countries/ })).toBeVisible();
});

test("leaving with unsaved edits asks first", async ({ page, stack }) => {
  await signIn(page, stack);
  await page.goto("/routes");
  const addSite = page.getByRole("link", { name: "Add site" }).first();
  await addSite.click();
  await page.getByRole("link", { name: "Cancel" }).click();
  await expect(page).toHaveURL(/\/routes$/);

  await addSite.click();
  await page.getByLabel("Hosts").fill("grafana.example.com");
  await page.getByLabel("Hosts").press("Enter");
  await page.getByRole("link", { name: "Cancel" }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByText("Discard your edits?")).toBeVisible();
  await dialog.getByRole("button", { name: "Keep editing" }).click();
  await expect(page).toHaveURL(/\/routes\/edit$/);
  await page.getByRole("link", { name: "Cancel" }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Discard edits" }).click();
  await expect(page).toHaveURL(/\/routes$/);
});
