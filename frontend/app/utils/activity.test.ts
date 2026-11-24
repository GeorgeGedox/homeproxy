import { describe, expect, it } from "vitest";
import { activityText, isActivity } from "~/utils/activity";

const entry = (action: string, details: Record<string, unknown> = {}) => ({ time: "2026-11-22T10:00:00Z", user: "admin", action, details });

describe("activity", () => {
  it.each([
    ["credentials.replace", { name: "cloudflare" }, "Credentials cloudflare replaced"],
    ["keys", { changed: ["crowdsec.bouncer_key", "geoip.maxmind_license_key"] }, "Keys changed: bouncer key, maxmind license key"],
    ["notification_urls", { changed: ["my_phone", "heartbeat_url"] }, "Notifications changed: my_phone, heartbeat URL"],
    ["dashboard_listen", { listen: "0.0.0.0:8443" }, "Dashboard address set to 0.0.0.0:8443"],
    ["security.decision.add", { value: "203.0.113.7", type: "ban", duration_seconds: 14_400 }, "203.0.113.7 banned for 4h"],
    ["security.decision.add", { value: "203.0.113.7", type: "captcha", duration_seconds: 3_600 }, "203.0.113.7 sent to a captcha for 1h"],
    ["security.decision.delete", { id: 7, value: "203.0.113.7" }, "203.0.113.7 unbanned"],
    ["security.decision.delete", { id: 7 }, "Decision 7 removed"],
    ["security.allowlist.add", { entry: "192.168.5.0/24" }, "192.168.5.0/24 always allowed"],
    ["cloudflare.purge", { deleted: ["app.example.com", "www.example.com"], errors: [] }, "Cloudflare site records removed: 2"],
    ["cloudflare.adopt_route", { host: "app.example.com", route: "app" }, "Cloudflare record app.example.com adopted for app"],
    ["something.new", {}, "something.new"],
  ])("%s %j reads %s", (action, details, text) => expect(activityText(entry(action, details))).toBe(text));

  it("leaves applies to the Applies tab", () => {
    expect([entry("apply"), entry("keys")].filter(isActivity).map((e) => e.action)).toEqual(["keys"]);
  });
});
