import type { components } from "~~/api/schema";

export type SeriesPoint = components["schemas"]["SeriesPoint"];
export interface Bucket { at: number; requests: number }

/**
 * Sum `points` into fixed buckets from `start` to `end`, zero-filled.
 * The API returns only buckets that saw traffic; a line drawn through those alone would skip the quiet hours.
 */
export function bucketSeries(points: SeriesPoint[], start: number, end: number, size: number): Bucket[] {
  const origin = start - (start % size);
  const buckets: Bucket[] = [];
  for (let at = origin; at < end; at += size) buckets.push({ at, requests: 0 });
  for (const point of points) {
    const index = Math.floor((Date.parse(point.at) - origin) / size);
    const bucket = buckets[index];
    if (bucket) bucket.requests += point.requests;
  }
  return buckets;
}

/** Total requests per group, largest first. Points with no group, such as a TCP line's status, make one row with group null. */
export function totalsByGroup(points: SeriesPoint[]): { group: string | null; requests: number }[] {
  const totals = new Map<string | null, number>();
  for (const point of points) {
    const key = point.group === null ? null : String(point.group);
    totals.set(key, (totals.get(key) ?? 0) + point.requests);
  }
  return [...totals].map(([group, requests]) => ({ group, requests })).sort((a, b) => b.requests - a.requests);
}

/** Axis ticks every `every` ms between `first` and `last`, on round local times: whole hours, local midnight. */
export function roundTicks(first: number, last: number, every: number): number[] {
  // getTimezoneOffset is UTC minus local time, so local midnight is a whole day plus it.
  const offset = new Date(first).getTimezoneOffset() * 60_000;
  const ticks: number[] = [];
  for (let at = Math.ceil((first - offset) / every) * every + offset; at <= last; at += every) ticks.push(at);
  return ticks;
}

/** Requests per status class (2xx to 5xx) from a series grouped by status. */
export function statusClasses(points: SeriesPoint[]): Record<"2xx" | "3xx" | "4xx" | "5xx", number> {
  const classes = { "2xx": 0, "3xx": 0, "4xx": 0, "5xx": 0 };
  for (const point of points) {
    const key = `${String(point.group).charAt(0)}xx`;
    if (key in classes) classes[key as keyof typeof classes] += point.requests;
  }
  return classes;
}
