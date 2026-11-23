const relative = new Intl.RelativeTimeFormat("en", { numeric: "auto" });
const count = new Intl.NumberFormat("en");
const STEPS: [Intl.RelativeTimeFormatUnit, number][] = [["day", 86_400], ["hour", 3_600], ["minute", 60]];

/** "3 hours ago", "in 2 days", or "just now" under a minute. */
export function timeAgo(iso: string, now = Date.now()): string {
  const seconds = (new Date(iso).getTime() - now) / 1000;
  for (const [unit, size] of STEPS) {
    if (Math.abs(seconds) >= size) return relative.format(Math.round(seconds / size), unit);
  }
  return "just now";
}

/** A moment as the dashboard writes it: "21 Nov, 14:05". */
export const dateTime = (at: string | number): string =>
  new Date(at).toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });

/** A running time in its two largest units: "3d 4h", "16m". */
export function duration(seconds: number): string {
  const units: [number, string][] = [
    [Math.floor(seconds / 86_400), "d"],
    [Math.floor(seconds / 3_600) % 24, "h"],
    [Math.floor(seconds / 60) % 60, "m"],
  ];
  const first = units.findIndex(([value]) => value > 0);
  if (first < 0) return `${Math.floor(seconds)}s`;
  return units.slice(first, first + 2).filter(([value]) => value > 0).map(([value, unit]) => `${value}${unit}`).join(" ");
}

/** A certificate's remaining life: "12 days left", "1 day left", or "expired" once `days_left` goes negative. */
export function daysLeft(days: number): string {
  if (days < 0) return "expired";
  return days === 1 ? "1 day left" : `${days} days left`;
}

/** One number format across the dashboard: 1,234. */
export const formatCount = (value: number): string => count.format(value);

const size = new Intl.NumberFormat("en", { maximumFractionDigits: 1 });
/** "512 B", "1.2 MB": decimal units, as a file manager shows them. */
export function formatBytes(bytes: number): string {
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1000 && unit < units.length - 1) {
    value /= 1000;
    unit += 1;
  }
  return `${size.format(value)} ${units[unit]}`;
}

const regions = new Intl.DisplayNames(["en"], { type: "region" });

/** A country's English name. A code Intl does not know, such as A1, throws there, so it shows as itself. */
export function countryName(code: string): string {
  try {
    return regions.of(code) ?? code;
  } catch {
    return code;
  }
}
