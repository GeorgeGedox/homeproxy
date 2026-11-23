import { addDomain, addSite, apply, expect, signIn, test } from "./fixtures";
import { viaHaproxy } from "./stack";

test("each apply shows what it changed in plain words, and a restore brings an older config back", async ({ page, stack }) => {
  await signIn(page, stack);
  await addDomain(page, "example.co");
  await addSite(page, stack, "app.example.co");
  await apply(page);
  await addSite(page, stack, "www.example.co");
  await apply(page);
  expect((await viaHaproxy(stack, "http://www.example.co/")).status).toBe(200);

  await page.goto("/history");
  const rows = page.getByRole("region", { name: "Applies" }).getByRole("listitem");
  const newest = rows.first();
  const older = rows.filter({ hasText: "Route app.example.co added" });
  await expect(newest).toContainText("Route www.example.co added");
  await expect(newest).toContainText("Live now");
  await expect(newest.getByRole("button", { name: "Restore" })).toHaveCount(0);
  await expect(older).toContainText("Domain example.co added");
  await expect(older).not.toContainText("example-co");

  await older.getByRole("button", { name: "Show changes" }).click();
  await expect(older).toContainText("www.example.co");

  // A staged edit is replaced by the restore, and the dialog says so first.
  await addSite(page, stack, "extra.example.co");
  await page.goto("/history");
  await older.getByRole("button", { name: "Restore" }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText("It replaces what is staged now: 1 change");
  await dialog.getByRole("button", { name: "Stage restore" }).click();
  const bar = page.getByTestId("pending-bar");
  await expect(bar).toContainText("Route www.example.co removed");
  await expect(bar).not.toContainText("extra.example.co");
  await apply(page);
  expect((await viaHaproxy(stack, "http://www.example.co/")).status).not.toBe(200);
  expect((await viaHaproxy(stack, "http://app.example.co/")).status).toBe(200);

  // With nothing staged, History still applies the saved files. The restore's own toast goes first.
  await expect(page.getByText("Applied", { exact: true })).toHaveCount(0, { timeout: 15_000 });
  await page.getByRole("button", { name: "Apply saved files" }).click();
  await expect(page.getByText("Applied", { exact: true })).toBeVisible();
});
