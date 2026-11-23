import { describe, expect, it, vi } from "vitest";
import { apiErrorMessage, apiFieldError, createApiClient } from "~/utils/api";

const failure = (message: string, field: string | null) => ({ error: { code: "validation_failed", message, field } });

describe("apiFieldError", () => {
  it("puts the first validation error on its field, without list indexes", () => {
    const error = failure("hosts.0: must be a DNS name; upstreams: needs at least one value", "hosts.0");
    expect(apiFieldError(error, "x")).toEqual({ field: "hosts", message: "Must be a DNS name." });
  });
  it("cuts a field at a key, so a header error lands on its list", () => {
    expect(apiFieldError(failure("request_headers.<key>: must be printable", "request_headers.<key>"), "x").field).toBe("request_headers");
  });
  it("keeps a message that does not start with its field", () => {
    expect(apiFieldError(failure("Move to another port first.", "listen"), "x")).toEqual({ field: "listen", message: "Move to another port first." });
  });
  it("has no field when the API names none", () => {
    expect(apiFieldError(failure("The lock is busy.", null), "x")).toEqual({ message: "The lock is busy." });
    expect(apiFieldError(undefined, "Not saved.")).toEqual({ message: "Not saved." });
  });
});

describe("createApiClient", () => {
  it("answers a request that never reached the API as an API error", async () => {
    vi.stubGlobal("fetch", () => Promise.reject(new TypeError("Failed to fetch")));
    const { data, error, response } = await createApiClient("http://192.168.1.10").GET("/api/v1/status");
    vi.unstubAllGlobals();
    expect([data, response.status, apiErrorMessage(error, "x")]).toEqual([undefined, 502, "The server is not reachable."]);
  });
});
