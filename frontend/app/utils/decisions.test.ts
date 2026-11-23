import { describe, expect, it } from "vitest";
import { banSummary } from "./decisions";

describe("banSummary", () => {
  it.each([
    [{ bans: 0, captchas: 0, blocklist: false }, "0 bans"],
    [{ bans: 1, captchas: 1, blocklist: false }, "1 ban · 1 captcha"],
    [{ bans: 2, captchas: 0, blocklist: true }, "2 bans · community list"],
  ])("words your own decisions, and a loaded blocklist", (counts, text) => {
    expect(banSummary(counts)).toBe(text);
  });
});
