import { describe, expect, it } from "vitest";
import { bucketSeries, roundTicks, statusClasses, totalsByGroup } from "~/utils/traffic";

const at = (minute: number) => new Date(Date.UTC(2026, 10, 21, 10, minute)).toISOString();
const start = Date.parse(at(0));
const MIN = 60_000;

describe("traffic", () => {
  it("zero-fills buckets and sums the points inside each", () => {
    const points = [{ at: at(1), group: null, requests: 2 }, { at: at(14), group: null, requests: 3 }, { at: at(40), group: null, requests: 1 }];
    expect(bucketSeries(points, start, start + 60 * MIN, 15 * MIN).map((b) => b.requests)).toEqual([5, 0, 1, 0]);
  });
  it("drops points outside the range", () => {
    expect(bucketSeries([{ at: at(59), group: null, requests: 4 }], start, start + 30 * MIN, 15 * MIN).map((b) => b.requests)).toEqual([0, 0]);
  });
  it("totals groups largest first, with ungrouped points as one row of their own", () => {
    const points = [{ at: at(0), group: "DE", requests: 1 }, { at: at(1), group: "US", requests: 3 }, { at: at(2), group: "DE", requests: 1 }, { at: at(3), group: null, requests: 2 }, { at: at(4), group: null, requests: 7 }];
    expect(totalsByGroup(points)).toEqual([{ group: null, requests: 9 }, { group: "US", requests: 3 }, { group: "DE", requests: 2 }]);
  });
  it("puts day ticks on local midnight and hour ticks on whole hours", () => {
    const DAY = 24 * 60 * MIN;
    const days = roundTicks(start, start + 5 * DAY, DAY);
    expect(days).toHaveLength(5);
    expect(days.every((tick) => new Date(tick).getHours() === 0 && new Date(tick).getMinutes() === 0)).toBe(true);
    const hours = roundTicks(start + 7 * MIN, start + 13 * 60 * MIN, 4 * 60 * MIN);
    expect(hours.every((tick) => new Date(tick).getMinutes() === 0)).toBe(true);
    expect(hours.slice(1).map((tick, i) => tick - (hours[i] ?? 0))).toEqual([4 * 60 * MIN, 4 * 60 * MIN]);
  });
  it("sorts status codes into classes and ignores lines without one", () => {
    const points = [{ at: at(0), group: 200, requests: 4 }, { at: at(0), group: 301, requests: 1 }, { at: at(0), group: 502, requests: 2 }, { at: at(0), group: null, requests: 7 }];
    expect(statusClasses(points)).toEqual({ "2xx": 4, "3xx": 1, "4xx": 0, "5xx": 2 });
  });
});
