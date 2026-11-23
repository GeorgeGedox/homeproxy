import { addDomain, addSite, apply, expect, signIn, test } from "./fixtures";
import { viaHaproxy } from "./stack";

const SITE = "http://app.example.io";

test.describe.configure({ mode: "serial" });

test("requests show live with their site, Pause holds new ones back, and the site filter keeps one site", async ({ page, stack }) => {
  await signIn(page, stack);
  await addDomain(page, "example.io");
  await addSite(page, stack, "app.example.io");
  await apply(page);

  await page.goto("/logs");
  const requests = page.getByRole("region", { name: "Requests" });
  await expect(requests).toContainText("Live");
  const lines = requests.getByRole("list", { name: "Access lines" });
  await viaHaproxy(stack, `${SITE}/first`);
  const first = lines.getByRole("listitem").filter({ hasText: "GET /first" });
  await expect(first).toContainText("app.example.io");
  // A click shows the line as HAProxy wrote it, with the HTTP version the row leaves out.
  await first.getByRole("button").click();
  await expect(first).toContainText("GET /first HTTP/1.1");

  await requests.getByRole("button", { name: "Pause" }).click();
  await viaHaproxy(stack, `${SITE}/held`);
  await expect(requests).toContainText("Paused · 1 new");
  await expect(lines).not.toContainText("GET /held");
  await requests.getByRole("button", { name: "Resume" }).click();
  await expect(lines.getByRole("listitem").first()).toContainText("GET /held");

  await viaHaproxy(stack, "http://nowhere.example.io/other");
  await expect(lines).toContainText("GET /other");
  await requests.getByRole("combobox", { name: "Site" }).click();
  await page.getByRole("option", { name: "app.example.io", exact: true }).click();
  await expect(lines).toContainText("GET /held");
  await expect(lines).not.toContainText("GET /other");
});

test("a refused filter says which and why, and the stored logs open with their download", async ({ page, stack }) => {
  await signIn(page, stack);
  await page.goto("/logs");
  const requests = page.getByRole("region", { name: "Requests" });
  // A text filter applies on Enter, so a half-typed value is never sent: the tail stays live meanwhile.
  await requests.getByLabel("Status").fill("99");
  await viaHaproxy(stack, `${SITE}/typing`);
  await expect(requests.getByRole("list", { name: "Access lines" })).toContainText("GET /typing");
  await requests.getByLabel("Status").press("Enter");
  await expect(requests).toContainText("Status: must be at least 100");
  await expect(requests).toContainText("Stopped");

  await page.getByRole("tab", { name: "HAProxy" }).click();
  const haproxy = page.getByRole("region", { name: "HAProxy messages" });
  await expect(haproxy).toContainText("Proxy");
  await expect(haproxy.getByRole("link", { name: "Download" })).toHaveAttribute("href", "/api/v1/logs/files/haproxy");
});
