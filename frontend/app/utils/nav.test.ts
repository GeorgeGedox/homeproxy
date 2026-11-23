import { describe, expect, it } from "vitest";
import { isActive } from "~/utils/nav";

describe("isActive", () => {
  const routes = { label: "Routes", icon: "lucide:route", to: "/routes" };
  const home = { label: "Overview", icon: "lucide:layout-dashboard", to: "/" };
  it("marks nested paths active", () => {
    expect(isActive(routes, "/routes")).toBe(true);
    expect(isActive(routes, "/routes/web")).toBe(true);
    expect(isActive(routes, "/routesx")).toBe(false);
  });
  it("matches Overview only on the root", () => {
    expect(isActive(home, "/")).toBe(true);
    expect(isActive(home, "/routes")).toBe(false);
  });
});
