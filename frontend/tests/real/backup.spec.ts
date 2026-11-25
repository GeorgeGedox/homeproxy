import { readFileSync } from "node:fs";
import { expect, logIn, signIn, test } from "./fixtures";

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
  const state = region.getByTestId("backup-token-state");
  await expect(state).toHaveText("Not set");
  await region.getByRole("button", { name: "Use the token" }).click();
  await expect(region.getByText('--pinnedpubkey "sha256//<key pin>"')).toBeVisible();
  // The image serves the dashboard with its own certificate, so the pin of that key shows: base64 of a SHA-256.
  await expect(region.getByTestId("backup-key-pin")).toHaveText(/^[A-Za-z0-9+/]{43}=$/);

  await region.getByRole("button", { name: "Create token" }).click();
  const first = await region.getByTestId("backup-token").innerText();
  expect(first.length).toBeGreaterThan(20);
  await expect(region).toContainText("It is shown only once");

  // The token is gone after a reload, and only its state shows.
  await page.reload();
  await expect(state).toContainText("created");
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
  await expect(state).toHaveText("Not set");
  expect((await download(second)).status()).toBe(401);
});

test("Restore backup in Settings checks the file, replaces everything and signs everyone out", async ({ page, stack }, testInfo) => {
  await signIn(page, stack);
  await page.goto("/settings/backup");
  const download = page.waitForEvent("download");
  await page.getByRole("link", { name: "Download backup" }).click();
  const file = await download;
  // The saved download has no extension, and the file field takes only a .tar.gz.
  const archive = testInfo.outputPath(file.suggestedFilename());
  await file.saveAs(archive);

  // A change after the backup: a backup token.
  const state = page.getByRole("region", { name: "Backup token" }).getByTestId("backup-token-state");
  await expect(state).toHaveText("Not set");
  await page.getByRole("button", { name: "Create token" }).click();
  await expect(state).toContainText("created");

  const restore = page.getByRole("region", { name: "Restore backup" });
  await restore.getByLabel("Backup file").setInputFiles(archive);
  const summary = restore.getByTestId("backup-summary");
  await expect(summary).toContainText("Valid HomeProxy backup");
  await expect(summary).toContainText(/Backup token\s*Not set/);
  await restore.getByRole("button", { name: "Restore backup" }).click();
  const dialog = page.getByRole("dialog", { name: "Restore this backup?" });
  await expect(dialog.getByRole("link", { name: "Download backup" })).toBeVisible();
  await dialog.getByRole("button", { name: "Restore backup" }).click();
  await expect(page.getByRole("dialog", { name: "Backup restored" })).toContainText("password from the backup");

  // The dashboard restarts, so it can refuse the first loads, or answer once more before it stops.
  // The session ended, so it asks for the password.
  await expect(async () => {
    await page.goto("/settings/backup");
    await expect(page).toHaveURL(/\/login$/, { timeout: 2_000 });
    await logIn(page);
  }).toPass({ timeout: 60_000 });
  await page.goto("/settings/backup");
  await expect(state).toHaveText("Not set");
});
