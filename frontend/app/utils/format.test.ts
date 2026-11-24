import { describe, expect, it } from "vitest";
import { countryName, daysLeft, duration, formatBytes, timeAgo } from "~/utils/format";

describe("format", () => {
  const now = Date.parse("2026-11-21T12:00:00Z");
  it.each([
    ["2026-11-21T11:59:30Z", "just now"],
    ["2026-11-21T09:00:00Z", "3 hours ago"],
    ["2026-11-23T12:00:00Z", "in 2 days"],
  ])("timeAgo(%s) is %s", (iso, text) => expect(timeAgo(iso, now)).toBe(text));
  it.each([
    [42, "42s"],
    [980, "16m"],
    [3 * 86_400 + 4 * 3_600 + 5 * 60, "3d 4h"],
  ])("duration(%i) is %s", (seconds, text) => expect(duration(seconds)).toBe(text));
});

it.each([
  [0, "0 B"],
  [999, "999 B"],
  [1_234_567, "1.2 MB"],
])("formatBytes(%s) is %s", (bytes, text) => expect(formatBytes(bytes)).toBe(text));

describe("countryName", () => {
  it.each([["DE", "Germany"], ["A1", "A1"], ["LAN", "LAN"]])("names %s, and shows a code Intl rejects as itself", (code, name) => {
    expect(countryName(code)).toBe(name);
  });
});

it.each([[1, "1 day left"], [0, "0 days left"], [-3, "expired"]])("daysLeft(%i) is %s", (days, text) => {
  expect(daysLeft(days)).toBe(text);
});
