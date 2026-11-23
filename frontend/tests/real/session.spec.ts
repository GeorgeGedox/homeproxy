import { expect, logIn, signIn, test } from "./fixtures";

test("Log out ends the session, a wrong password is refused, and the right one signs in again", async ({ page, stack }) => {
  await signIn(page, stack);
  await page.getByRole("button", { name: "Log out" }).click();
  await expect(page).toHaveURL(/\/login$/);
  // The session is gone, not only the page: a signed-in page sends the visitor back to log in.
  await page.goto("/routes");
  await expect(page).toHaveURL(/\/login$/);

  // One wrong try; login throttles from the sixth.
  await page.getByLabel("Password").fill("not the admin password");
  await page.getByRole("button", { name: "Log in" }).click();
  await expect(page.getByText("Wrong password.")).toBeVisible();
  await logIn(page);
  await expect(page.getByRole("heading", { name: "Overview" })).toBeVisible();
});
