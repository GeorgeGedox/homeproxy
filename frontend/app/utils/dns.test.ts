import { describe, expect, it } from "vitest";
import { dnsNames, forwardingLines } from "~/utils/dns";

const http = (name: string, hosts: string[], extra: Record<string, unknown> = {}) =>
  ({ name, type: "http", hosts, enabled: true, dns: { target: "haproxy" }, ...extra }) as unknown as Route;

describe("dnsNames", () => {
  it("answers route hosts with HomeProxy or their own target, and marks what the next apply changes", () => {
    const routes = [
      http("app", ["app.example.com"]),
      http("nas", ["nas.example.com"], { dns: { target: "192.168.1.30" } }),
      http("off", ["off.example.com"], { enabled: false }),
      { name: "game", type: "tcp", listen_port: 25565, enabled: true } as unknown as Route,
    ];
    const extra = [{ name: "printer.example.com", target: "192.168.1.40" }, { name: "app.example.com", target: "192.168.1.99" }];
    const live = { "app.example.com": "192.168.1.10", "nas.example.com": "192.168.1.10", "old.example.com": "192.168.1.50" };
    expect(dnsNames(routes, extra, "192.168.1.10", live)).toEqual([
      { name: "app.example.com", target: "192.168.1.10", route: "app", state: "live" },
      { name: "nas.example.com", target: "192.168.1.30", route: "nas", state: "staged", was: "192.168.1.10" },
      { name: "old.example.com", target: "192.168.1.50", state: "removing" },
      { name: "printer.example.com", target: "192.168.1.40", state: "staged" },
    ]);
  });
});

describe("forwardingLines", () => {
  it("writes one line for each zone in the AdGuard Home and dnsmasq formats", () => {
    expect(forwardingLines(["example.com", "example.org"], "192.168.1.10:5335")).toEqual({
      adguard: "[/example.com/]192.168.1.10:5335\n[/example.org/]192.168.1.10:5335",
      dnsmasq: "server=/example.com/192.168.1.10#5335\nserver=/example.org/192.168.1.10#5335",
    });
  });
});
