import { describe, expect, it } from "vitest";
import { guardRedirect } from "~/utils/session";

describe("guardRedirect", () => {
  it("sends first run to setup", () => {
    expect(guardRedirect("setup", "/routes")).toBe("/setup");
    expect(guardRedirect("setup", "/setup")).toBeNull();
  });
  it("sends anonymous visitors to login", () => {
    expect(guardRedirect("anonymous", "/routes")).toBe("/login");
    expect(guardRedirect("anonymous", "/login")).toBeNull();
  });
  it("keeps signed-in users off login and setup", () => {
    expect(guardRedirect("authenticated", "/login")).toBe("/");
    expect(guardRedirect("authenticated", "/setup")).toBe("/");
    expect(guardRedirect("authenticated", "/routes")).toBeNull();
  });
  it("does not redirect loop when the API is down", () => {
    expect(guardRedirect("unreachable", "/routes")).toBeNull();
  });
});
