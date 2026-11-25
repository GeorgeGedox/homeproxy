import type { Page } from "@playwright/test";
import { expect, routeApi, test } from "./fixtures";

// The normal flows (stage, details, apply, discard) run in tests/real. These are the states a real
// stack does not reach through its UI: a release haproxy -c refuses, and files edited on disk.
type Start = "staged" | "hand-edit" | "broken" | "conflict";

/** A mock backend with one staged route, a hand edit, a saved config that does not render, or staged files edited on disk. The first apply fails when `failFirst` is set. */
async function mockApi(page: Page, failFirst: boolean, start: Start = "staged") {
  let pending = start === "staged" || start === "conflict";
  let applies = 0;
  const review = () => ({
    changes: pending ? [{ path: "routes/app.yaml", kind: "write" }] : [],
    summary: pending ? [{ kind: "route", action: "added", label: "app.example.com" }] : [],
    conflict: start === "conflict",
    conflicting_paths: start === "conflict" ? ["routes/app.yaml"] : [],
    config_diff: pending ? "+++ routes/app.yaml\n+name: app" : "",
    generated_diff: start === "broken" ? null : pending || (start === "hand-edit" && !applies) ? "+backend app" : "",
    generated_error: start === "broken" ? "routes/app.yaml: hosts: example.org is not inside a domain" : null,
  });
  const fixed: Record<string, unknown> = {
    "/setup/status": { setup_required: false },
    "/auth/me": { authenticated: true },
    "/settings": { cloudflare: { enabled: false } },
  };
  await routeApi(page, (_, path) => {
    if (path in fixed) return [200, fixed[path]];
    if (path === "/changes") return [200, review()];
    if (path !== "/apply") return undefined;
    applies += 1;
    if (failFirst && applies === 1) {
      const errors = ["haproxy -c refused the new release"];
      const haproxy_log = "[NOTICE]   (1) : haproxy version is 3.2\n[ALERT]    (1) : config : parsing [/x/includes/bad.cfg:2] : unknown keyword 'bogus' in 'frontend' section\n";
      return [200, { success: false, stage: "check", errors, haproxy_log, warnings: [], certificate_jobs: [] }];
    }
    pending = false;
    return [200, { success: true, stage: "done", errors: [], haproxy_log: "", warnings: [], certificate_jobs: [] }];
  });
}

test("a failed apply keeps the bar with the error and the changes staged", async ({ page }) => {
  await mockApi(page, true);
  await page.goto("/");
  const bar = page.getByTestId("pending-bar");
  await bar.getByRole("button", { name: "Apply" }).click();
  await expect(bar).toContainText("Apply failed at check");

  await bar.getByRole("button", { name: "Details" }).click();
  const details = page.getByRole("dialog");
  await expect(details.getByText("haproxy -c refused the new release")).toBeVisible();
  await expect(details.getByText("parsing [/x/includes/bad.cfg:2] : unknown keyword 'bogus'")).toBeVisible();
  await expect(details.getByText("haproxy version is")).toHaveCount(0);
  await expect(details.getByText("Route app.example.com added")).toBeVisible();
});

test("with nothing staged, a hand edit still shows the bar and applies", async ({ page }) => {
  await mockApi(page, false, "hand-edit");
  await page.goto("/");
  const bar = page.getByTestId("pending-bar");
  await expect(bar).toContainText("Saved config differs from what runs");
  await expect(bar.getByRole("button", { name: "Discard" })).toHaveCount(0);

  await bar.getByRole("button", { name: "Apply" }).click();
  await expect(page.getByText("Applied", { exact: true })).toBeVisible();
  await expect(bar).toHaveCount(0);
});

test("a saved config that does not render shows in the bar with its error", async ({ page }) => {
  await mockApi(page, false, "broken");
  await page.goto("/");
  const bar = page.getByTestId("pending-bar");
  await expect(bar).toContainText("The saved config does not render");
  await bar.getByRole("button", { name: "Details" }).click();
  await expect(page.getByRole("dialog")).toContainText("example.org is not inside a domain");
});

test("staged files edited on disk block Apply, and buttons show the hand cursor and the focus ring", async ({ page }) => {
  await mockApi(page, false, "conflict");
  await page.goto("/");
  const bar = page.getByTestId("pending-bar");
  const style = (name: string) => bar.getByRole("button", { name }).evaluate((el) => {
    const cs = getComputedStyle(el);
    return { cursor: cs.cursor, outline: `${cs.outlineStyle} ${cs.outlineWidth}` };
  });
  await expect(bar).toContainText("Staged files were edited on disk");
  await expect(bar.getByRole("button", { name: "Apply" })).toBeDisabled();
  expect((await style("Details")).cursor).toBe("pointer");
  expect((await style("Apply")).cursor).toBe("not-allowed");

  await bar.getByRole("button", { name: "Details" }).focus();
  await page.keyboard.press("Tab");
  await expect.poll(() => style("Discard")).toEqual({ cursor: "pointer", outline: "solid 2px" });
});
