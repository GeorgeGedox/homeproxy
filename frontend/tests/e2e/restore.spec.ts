import type { Page } from "@playwright/test";
import { expect, routeApi, test, type Reply } from "./fixtures";

// A real setup restore needs a stack that is not set up yet, and it restarts the dashboard with a new
// certificate. The real stacks start before the browsers and are set up by the first test, so the
// setup restore states come from a mock. A check refusal and a backup of an older HomeProxy need a
// crafted archive, so they come from the mock too. The real restore from Settings is in tests/real.

const status = { setup_required: true, detected: { network: { lan_cidrs: ["192.168.1.0/24"] } }, needs: [] };
const summary = {
  made_at: "2026-11-24T03:00:00Z", config_version: 1, migrated: true, routes: 4, domains: ["example.com"],
  certificates: 1, snapshots: 9, password_set: true, backup_token_set: false,
};
const refusal: Reply = [422, { error: { code: "not_a_backup", message: "This is not a HomeProxy backup.", field: null } }];
const file = { name: "backup.tar.gz", mimeType: "application/gzip", buffer: Buffer.from("abc") };

interface Sent { path: string; token?: string; type?: string; size?: number }

/** Answer the check and restore routes under `prefix` in turn from `answers`, and record what each request sent. */
async function routeArchive(page: Page, prefix: string, answers: Reply[]): Promise<Sent[]> {
  const sent: Sent[] = [];
  // The archive is not JSON, so these routes get their own handler on top of routeApi.
  const archiveRoute = (url: URL) => url.pathname.startsWith(`/api/v1${prefix}/`) && /\/(check|restore)$/.test(url.pathname);
  await page.route(archiveRoute, (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname.replace(`/api/v1${prefix}`, "");
    sent.push({ path, token: request.headers()["x-setup-token"], type: request.headers()["content-type"], size: request.postDataBuffer()?.length });
    const [code, data] = answers.shift() ?? [500];
    return route.fulfill({ status: code, contentType: "application/json", body: data ? JSON.stringify(data) : "" });
  });
  return sent;
}

test("Restore from backup checks the raw file with the setup token, shows each refusal and the summary, then the next steps in place of the form", async ({ page }) => {
  await routeApi(page, (method, path) => (path === "/setup/status" ? [200, status] : undefined));
  const sent = await routeArchive(page, "/setup", [
    [403, { error: { code: "invalid_setup_token", message: "The setup token is wrong.", field: null } }],
    refusal,
    [200, summary],
    [204],
  ]);
  await page.goto("/setup");
  await page.getByRole("tab", { name: "Restore from backup" }).click();
  const restore = page.getByRole("tabpanel", { name: "Restore from backup" });
  await restore.getByRole("button", { name: "Check backup" }).click();
  await expect(restore).toContainText("Enter the setup token first.");
  await page.getByLabel("Setup token").fill("some-token");
  await restore.getByRole("button", { name: "Check backup" }).click();
  await expect(restore).toContainText("Choose the backup file.");

  // Choosing the file checks it at once. The button checks it again, for example after a wrong token.
  await restore.getByLabel("Backup file").setInputFiles(file);
  await expect(restore).toContainText("The setup token is wrong.");
  await restore.getByRole("button", { name: "Check backup" }).click();
  await expect(restore).toContainText("This is not a HomeProxy backup.");
  await expect(restore.getByRole("button", { name: "Restore backup" })).toHaveCount(0);
  await restore.getByRole("button", { name: "Check backup" }).click();
  await expect(restore.getByTestId("backup-summary")).toContainText("from an older HomeProxy, updated on restore");

  await restore.getByRole("button", { name: "Restore backup" }).click();
  const done = page.getByRole("region", { name: "Backup restored" });
  await expect(done).toContainText("certificate from the backup");
  await expect(done).toContainText("warns about the certificate again");
  await expect(done).toContainText("password from the backup");
  await expect(page.getByRole("button", { name: "Finish setup" })).toHaveCount(0);
  const request = { token: "some-token", type: "application/gzip", size: 3 };
  expect(sent).toEqual([...Array(3).fill({ path: "/restore/check", ...request }), { path: "/restore", ...request }]);
});

test("Restore backup in Settings shows a refused file and a backup of an older HomeProxy before the restore", async ({ page }) => {
  await routeApi(page, (method, path) => {
    if (path === "/auth/me") return [200, { user: "admin" }];
    if (path === "/backup/token") return [200, { set: false, key_pin: null }];
    return undefined;
  });
  const sent = await routeArchive(page, "/backup", [refusal, [200, summary]]);
  await page.goto("/settings/backup");
  const restore = page.getByRole("region", { name: "Restore backup" });
  await restore.getByLabel("Backup file").setInputFiles(file);
  await expect(restore).toContainText("This is not a HomeProxy backup.");
  await expect(restore.getByRole("button", { name: "Restore backup" })).toHaveCount(0);

  await restore.getByLabel("Backup file").setInputFiles({ ...file, name: "other.tar.gz" });
  await expect(restore.getByTestId("backup-summary")).toContainText("from an older HomeProxy, updated on restore");
  await expect(restore).not.toContainText("This is not a HomeProxy backup.");
  expect(sent).toEqual(Array(2).fill({ path: "/check", type: "application/gzip", size: 3 }));
});
