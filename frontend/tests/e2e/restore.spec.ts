import { expect, routeApi, test, type Reply } from "./fixtures";

// A real restore needs a stack that is not set up yet, and it restarts the dashboard with a new
// certificate. The real stacks start before the browsers and are set up by the first test, so the
// restore states come from a mock.

const status = { setup_required: true, detected: { network: { lan_cidrs: ["192.168.1.0/24"] } }, needs: [] };

test("Restore from backup sends the raw file with the setup token, shows each refusal, then the next steps in place of the form", async ({ page }) => {
  const sent: { token?: string; type?: string; size?: number }[] = [];
  const answers: Reply[] = [
    [403, { error: { code: "invalid_setup_token", message: "The setup token is wrong.", field: null } }],
    [422, { error: { code: "not_a_backup", message: "This is not a HomeProxy backup.", field: null } }],
    [204],
  ];
  await routeApi(page, (method, path) => (path === "/setup/status" ? [200, status] : undefined));
  await page.route("**/api/v1/setup/restore", (route) => {
    const request = route.request();
    sent.push({ token: request.headers()["x-setup-token"], type: request.headers()["content-type"], size: request.postDataBuffer()?.length });
    const [code, data] = answers.shift() ?? [500];
    return route.fulfill({ status: code, contentType: "application/json", body: data ? JSON.stringify(data) : "" });
  });
  await page.goto("/setup");
  await page.getByRole("tab", { name: "Restore from backup" }).click();
  const restore = page.getByRole("tabpanel", { name: "Restore from backup" });
  await restore.getByRole("button", { name: "Restore backup" }).click();
  await expect(restore).toContainText("Enter the setup token first.");
  await page.getByLabel("Setup token").fill("some-token");
  await restore.getByRole("button", { name: "Restore backup" }).click();
  await expect(restore).toContainText("Choose the backup file.");

  await restore.getByLabel("Backup file").setInputFiles({ name: "backup.tar.gz", mimeType: "application/gzip", buffer: Buffer.from("abc") });
  await restore.getByRole("button", { name: "Restore backup" }).click();
  await expect(restore).toContainText("The setup token is wrong.");
  await restore.getByRole("button", { name: "Restore backup" }).click();
  await expect(restore).toContainText("This is not a HomeProxy backup.");
  await restore.getByRole("button", { name: "Restore backup" }).click();
  const done = page.getByRole("region", { name: "Backup restored" });
  await expect(done).toContainText("certificate from the backup");
  await expect(done).toContainText("warns about the certificate again");
  await expect(done).toContainText("password from the backup");
  await expect(page.getByRole("button", { name: "Finish setup" })).toHaveCount(0);
  expect(sent).toEqual(Array(3).fill({ token: "some-token", type: "application/gzip", size: 3 }));
});
