import { readFileSync } from "node:fs";
import { expect, signIn, test } from "./fixtures";

test.describe.configure({ mode: "serial" });

test("Download backup saves a gzip archive named for the version", async ({ page, stack }) => {
  await signIn(page, stack);
  await page.goto("/settings/backup");
  const download = page.waitForEvent("download");
  await page.getByRole("link", { name: "Download backup" }).click();
  const file = await download;
  expect(file.suggestedFilename()).toMatch(/^homeproxy-backup-.+\.tar\.gz$/);
  // A gzip stream starts with 1f 8b.
  expect([...readFileSync(String(await file.path())).subarray(0, 2)]).toEqual([0x1f, 0x8b]);
});

test("the backup token is shown once, replaced and removed only after a confirmation", async ({ page, stack, playwright }) => {
  await signIn(page, stack);
  await page.goto("/settings/backup");
  const region = page.getByRole("region", { name: "Backup token" });
  await expect(region).toContainText("No backup token is set.");
  await expect(region).toContainText("--cacert homeproxy.pem");
  await expect(region).not.toContainText("--insecure");

  await region.getByRole("button", { name: "Create token" }).click();
  const first = await region.getByTestId("backup-token").innerText();
  expect(first.length).toBeGreaterThan(20);
  await expect(region).toContainText("It is shown only once");

  // The token is gone after a reload, and only its state shows.
  await page.reload();
  await expect(region).toContainText("A backup token is set");
  await expect(region.getByTestId("backup-token")).toHaveCount(0);

  // Replace asks first. Cancel keeps the old token.
  await region.getByRole("button", { name: "Replace token" }).click();
  const dialog = page.getByRole("dialog", { name: "Replace the backup token?" });
  await dialog.getByRole("button", { name: "Cancel" }).click();
  await expect(region.getByTestId("backup-token")).toHaveCount(0);
  // A script has no session cookie, so it gets its own context.
  const script = await playwright.request.newContext({ baseURL: stack.dashboard, ignoreHTTPSErrors: true });
  const download = (token: string) => script.get("/api/v1/backup", { headers: { Authorization: `Bearer ${token}` } });
  expect((await download(first)).status()).toBe(200);

  await region.getByRole("button", { name: "Replace token" }).click();
  await dialog.getByRole("button", { name: "Replace token" }).click();
  const second = await region.getByTestId("backup-token").innerText();
  expect(second).not.toBe(first);
  expect((await download(first)).status()).toBe(401);
  expect((await download(second)).status()).toBe(200);

  await region.getByRole("button", { name: "Remove token" }).click();
  await page.getByRole("dialog", { name: "Remove the backup token?" }).getByRole("button", { name: "Remove token" }).click();
  await expect(region).toContainText("No backup token is set.");
  expect((await download(second)).status()).toBe(401);
});
