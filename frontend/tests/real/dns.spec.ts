import { addDomain, addSite, apply, expect, signIn, test } from "./fixtures";
import { resolveOnLan } from "./stack";

test.describe.configure({ mode: "serial" });

test("a site's host answers with HomeProxy's address, and an extra record with its own", async ({ page, stack }) => {
  await signIn(page, stack);
  await addDomain(page, "example.edu");
  await addSite(page, stack, "app.example.edu");

  await page.goto("/dns");
  await page.getByRole("button", { name: "Add record" }).click();
  await page.getByLabel("Name", { exact: true }).fill("nas.example.edu");
  await page.getByLabel("Address").fill("192.168.1.30");
  await page.getByRole("button", { name: "Stage change" }).click();
  const names = page.getByRole("region", { name: "Names it answers" });
  await expect(names.getByRole("listitem").filter({ hasText: "nas.example.edu" })).toContainText("staged");
  await apply(page);

  await expect(page.getByRole("region", { name: "DNS server" })).toContainText("Answering");
  // Setup forwards other names to a resolver, so the light is not the yellow "only the names below".
  await expect(page.getByRole("region", { name: "DNS server" })).not.toContainText("only the names");
  await expect(page.getByRole("region", { name: "DNS server" })).toContainText("[/example.edu/]");
  const site = names.getByRole("listitem").filter({ hasText: "app.example.edu" });
  await expect(site).not.toContainText("staged");
  const homeproxy = (await site.innerText()).match(/→\s*(\S+)/)?.[1];
  expect(await resolveOnLan(stack, "app.example.edu")).toEqual([homeproxy]);
  expect(await resolveOnLan(stack, "nas.example.edu")).toEqual(["192.168.1.30"]);
});

test("a removed extra record stays until the apply, then the server stops answering it", async ({ page, stack }) => {
  await signIn(page, stack);
  await page.goto("/dns");
  await page.getByRole("button", { name: "Remove nas.example.edu" }).click();
  const row = page.getByRole("region", { name: "Names it answers" }).getByRole("listitem").filter({ hasText: "nas.example.edu" });
  await expect(row).toContainText("removed at the next apply");
  expect(await resolveOnLan(stack, "nas.example.edu")).toEqual(["192.168.1.30"]);
  await apply(page);
  await expect(row).toHaveCount(0);
  expect(await resolveOnLan(stack, "nas.example.edu")).toEqual([]);
});
