import { addDomain, addSite, apply, expect, signIn, test } from "./fixtures";
import { viaHaproxy } from "./stack";

// The actions on the Routes list, on sites of their own domain. They build on each other in one stack.
test.describe.configure({ mode: "serial" });

const DOCS = "http://docs.example.dev";

test("a site's exposure switch stages each of its paths, and switching back unstages them", async ({ page, stack }) => {
  await signIn(page, stack);
  await addDomain(page, "example.dev");
  await addSite(page, stack, "wiki.example.dev");
  await addSite(page, stack, "wiki.example.dev", "/api");
  await apply(page);

  const site = page.getByRole("region", { name: "wiki.example.dev" });
  const exposure = (option: string) => site.getByRole("group", { name: "Who can reach wiki.example.dev" }).getByRole("button", { name: option });
  await expect(exposure("LAN only")).toHaveAttribute("aria-pressed", "true");
  await exposure("Public").click();
  const bar = page.getByTestId("pending-bar");
  await expect(bar).toContainText("2 changes staged");
  await page.reload();
  await expect(exposure("Public")).toHaveAttribute("aria-pressed", "true");
  // Cloudflare is off, so a public site has no Cloudflare switch.
  await expect(site.getByRole("switch", { name: /Cloudflare DNS record/ })).toHaveCount(0);

  await exposure("LAN only").click();
  await expect(bar).toHaveCount(0);
});

test("a TCP port's exposure switch stages that one route", async ({ page, stack }) => {
  await signIn(page, stack);
  await page.goto("/routes");
  await page.getByRole("link", { name: "Add site" }).first().click();
  await page.getByRole("radio", { name: /TCP port/ }).click();
  await page.getByLabel("Port", { exact: true }).fill("2222");
  await page.getByLabel("Upstreams").fill(`${stack.upstream}:80`);
  await page.getByLabel("Upstreams").press("Enter");
  await page.getByRole("button", { name: "Stage change" }).click();
  await apply(page);

  const exposure = (option: string) => page.getByRole("group", { name: "Who can reach Port 2222" }).getByRole("button", { name: option });
  await exposure("Public").click();
  const bar = page.getByTestId("pending-bar");
  await expect(bar).toContainText("1 change staged");
  await page.reload();
  await expect(exposure("Public")).toHaveAttribute("aria-pressed", "true");
  await exposure("LAN only").click();
  await expect(bar).toHaveCount(0);
});

test("Sticky upstream saves on and off on an HTTP site and on a TCP port", async ({ page, stack }) => {
  await signIn(page, stack);
  await addSite(page, stack, "pve.example.dev");
  await page.getByRole("link", { name: "Add site" }).first().click();
  await page.getByRole("radio", { name: /TCP port/ }).click();
  await page.getByLabel("Port", { exact: true }).fill("2223");
  await page.getByLabel("Upstreams").fill(`${stack.upstream}:80`);
  await page.getByLabel("Upstreams").press("Enter");
  await page.getByRole("button", { name: "Stage change" }).click();
  await apply(page);

  const sticky = page.getByRole("switch", { name: "Sticky upstream" });
  const rows = [
    { region: "pve.example.dev", row: /^\/\s*→/ },
    { region: "Port 2223", row: /^TCP\s*→/ },
  ];
  for (const { region, row } of rows) {
    for (const on of [true, false]) {
      await page.goto("/routes");
      await page.getByRole("region", { name: region, exact: true }).getByRole("link", { name: row }).click();
      if (on) {
        // With one upstream there is nothing to stick to, so the switch shows only with a second one.
        await expect(sticky).toBeHidden();
        await page.getByLabel("Upstreams").fill(`${stack.upstream}:81`);
        await page.getByLabel("Upstreams").press("Enter");
      }
      await expect(sticky).toBeChecked({ checked: !on });
      await sticky.click();
      await page.getByRole("button", { name: "Stage change" }).click();
      await apply(page);

      await page.goto("/routes");
      await page.getByRole("region", { name: region, exact: true }).getByRole("link", { name: row }).click();
      await expect(sticky).toBeChecked({ checked: on });
    }
  }
});

test("an apply's warning is counted in its toast and stays listed on Overview", async ({ page, stack }) => {
  await signIn(page, stack);
  await addSite(page, stack, "docs.example.dev", "/guide");
  await apply(page);

  // The toast only counts the warnings; Overview lists them.
  await expect(page.getByText(/^Applied with (a warning|\d+ warnings)$/)).toBeVisible();
  await page.getByRole("link", { name: "View on Overview" }).click();
  await expect(page.getByRole("region", { name: /^Needs attention/ })).toContainText("docs.example.dev is routed only on /guide");
});

test("a host routed only on a path offers Add /, which makes it answer on /", async ({ page, stack }) => {
  await signIn(page, stack);
  await page.goto("/routes");
  const site = page.getByRole("region", { name: "docs.example.dev" });
  await expect(site).toContainText("docs.example.dev is routed only on /guide");
  await site.getByRole("link", { name: "Add /" }).click();
  await expect(page.getByRole("heading", { name: "Add path to docs.example.dev" })).toBeVisible();
  await expect(page.getByLabel("Path", { exact: true })).toHaveValue("/");
  await page.getByLabel("Upstreams").fill(`${stack.upstream}:80`);
  await page.getByLabel("Upstreams").press("Enter");
  await page.getByRole("button", { name: "Stage change" }).click();
  await apply(page);

  await expect(site.getByText("routed only on")).toHaveCount(0);
  expect((await viaHaproxy(stack, `${DOCS}/`)).status).toBe(200);
});

test("Duplicate starts a new site from a path's settings", async ({ page, stack }) => {
  await signIn(page, stack);
  await page.goto("/routes");
  await page.getByRole("button", { name: "Actions for docs.example.dev/guide", exact: true }).click();
  await page.getByRole("menuitem", { name: "Duplicate" }).click();
  await expect(page.getByRole("heading", { name: "Add site" })).toBeVisible();
  await expect(page.getByLabel("Path", { exact: true })).toHaveValue("/guide");
  await expect(page.getByText(`${stack.upstream}:80`)).toBeVisible();
  await page.getByRole("link", { name: "Cancel" }).click();
  await expect(page).toHaveURL(/\/routes$/);
});

test("a path switched off stops answering at the apply, and a deleted site stops answering too", async ({ page, stack }) => {
  await signIn(page, stack);
  await page.goto("/routes");
  await page.getByRole("switch", { name: "Enabled docs.example.dev", exact: true }).click();
  await apply(page);
  expect((await viaHaproxy(stack, `${DOCS}/`)).status).not.toBe(200);
  expect((await viaHaproxy(stack, `${DOCS}/guide`)).status).toBe(200);

  for (const target of ["docs.example.dev", "docs.example.dev/guide"]) {
    await page.getByRole("button", { name: `Actions for ${target}`, exact: true }).click();
    await page.getByRole("menuitem", { name: "Delete" }).click();
    await page.getByRole("dialog").getByRole("button", { name: "Delete" }).click();
  }
  await apply(page);
  await expect(page.getByRole("region", { name: "docs.example.dev" })).toHaveCount(0);
  expect((await viaHaproxy(stack, `${DOCS}/guide`)).status).not.toBe(200);
});
