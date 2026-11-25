import { describe, expect, it } from "vitest";
import { banSummary } from "./decisions";

describe("banSummary", () => {
  it.each([
    [{ bans: 0, captchas: 0, blocklist: 0, blocklist_updated_at: null }, "0 bans"],
    [{ bans: 1, captchas: 1, blocklist: 0, blocklist_updated_at: null }, "1 ban · 1 captcha"],
    [{ bans: 2, captchas: 0, blocklist: 1204, blocklist_updated_at: "2026-11-20T10:00:00Z" }, "2 bans · 1,204 from blocklists"],
  ])("words your own decisions, and a loaded blocklist", (counts, text) => {
    expect(banSummary(counts)).toBe(text);
  });
});
