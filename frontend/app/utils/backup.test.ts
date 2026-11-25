import { describe, expect, it } from "vitest";
import { backupCommands } from "./backup";

describe("backupCommands", () => {
  it("pins the certificate and never skips the check", () => {
    const text = backupCommands("192.168.1.10:8443");
    expect(text).toContain("--cacert homeproxy.pem --resolve homeproxy:8443:192.168.1.10");
    expect(text).toContain("https://homeproxy:8443/api/v1/backup");
    expect(text).not.toMatch(/--insecure|\s-k\b/);
  });
});
