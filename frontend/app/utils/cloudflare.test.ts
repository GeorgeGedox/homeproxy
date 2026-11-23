import { describe, expect, it } from "vitest";
import { cloudflareStatus, zoneRows, type CloudflareState } from "~/utils/cloudflare";

const record = (name: string, content: string, comment = `managed-by:homeproxy route:${name}`) => ({ id: name, type: "CNAME", name, content, proxied: true, comment, ttl: 1 });
const change = (action: "create" | "update" | "delete", name: string, extra: object = {}) =>
  ({ action, name, content: "home.example.com", proxied: true, route: "r", record_id: "", old_content: "", old_proxied: false, ...extra });

describe("zoneRows", () => {
  it("lists site records without the DDNS record, with waiting changes and hand-made conflicts merged in", () => {
    const zone = {
      zone: "example.com",
      records: [record("home.example.com", "203.0.113.7", "managed-by:homeproxy ddns:example.com"), record("app.example.com", "home.example.com"), record("old.example.com", "home.example.com"), record("cam.example.com", "home.example.com"), record("www.example.com", "home.example.com")],
      changes: [
        change("create", "new.example.com"),
        // A removal shows the record as it is now, not as the change describes it.
        change("delete", "old.example.com", { proxied: false }),
        change("update", "cam.example.com", { proxied: false, old_content: "home.example.com", old_proxied: true }),
        change("update", "app.example.com", { old_content: "elsewhere.example.com", old_proxied: true }),
      ],
      conflicts: [{ name: "nas.example.com", route: "nas" }],
    };
    expect(zoneRows(zone)).toEqual([
      { name: "app.example.com", content: "home.example.com", proxied: true, state: "changing", was: "elsewhere.example.com" },
      { name: "cam.example.com", content: "home.example.com", proxied: false, state: "changing", was: "proxied" },
      { name: "nas.example.com", content: "", proxied: false, state: "hand-made" },
      { name: "new.example.com", content: "home.example.com", proxied: true, state: "adding" },
      { name: "old.example.com", content: "home.example.com", proxied: true, state: "removing" },
      { name: "www.example.com", content: "home.example.com", proxied: true, state: "live" },
    ]);
  });
});

describe("cloudflareStatus", () => {
  const base: CloudflareState = { enabled: true, zones: [], last_reconcile: null, ddns: [], ddns_checked_at: null };
  const ddns = (action: CloudflareState["ddns"][number]["action"]) => ({ zone: "example.com", record: "home.example.com", action, content: "", error: "" });
  it.each([
    ["off", { enabled: false, last_reconcile: { at: "2026-11-21T10:00:00Z", error: "refused" } }, "off"],
    ["a refused sync", { last_reconcile: { at: "2026-11-21T10:00:00Z", error: "refused" } }, "down"],
    ["a sync that could not write", { zones: [{ zone: "example.com" }], last_reconcile: { at: "2026-11-21T10:00:00Z", error: "", zones: [{ zone: "example.com", errors: ["app.example.com: 403"] }] } }, "down"],
    ["a failed DDNS update", { ddns: [ddns("failed")], zones: [{ zone: "example.com", conflicts: [{ name: "a", route: "a" }] }] }, "down"],
    ["a hand-made DDNS record", { ddns: [ddns("hand-made")], zones: [{ zone: "example.com", changes: [change("create", "a")] }] }, "unknown"],
    ["changes waiting", { zones: [{ zone: "example.com", changes: [change("create", "a")] }] }, "staged"],
    ["no Cloudflare domain", {}, "off"],
    ["nothing to do", { ddns: [ddns("unchanged")], zones: [{ zone: "example.com" }] }, "up"],
  ] as const)("shows %s, the worst state first", (_, state, tone) => {
    expect(cloudflareStatus({ ...base, ...state } as CloudflareState).tone).toBe(tone);
  });
});
