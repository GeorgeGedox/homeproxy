import type { LedTone } from "~/utils/health";

export type MetricsGroup = "route" | "status" | "country";
type Range = "1h" | "6h" | "24h" | "7d" | "30d" | "90d";
/** One value of a group, as a ranked row: `value` is null for lines that had none, such as a TCP line's status. */
export interface RankedRow { group: MetricsGroup; value: string | null; label: string; requests: number; tone?: LedTone }

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;
/**
 * Each range is drawn in 60 to 170 buckets, the API's minutes or hours summed, so quiet traffic does not read as spikes.
 * `per` names the bucket for the axis, and `tick` spaces the axis labels.
 */
export const RANGES: { label: string; value: Range; ms: number; step: number; per: string; tick: number }[] = [
  { label: "1h", value: "1h", ms: HOUR, step: MINUTE, per: "minute", tick: 10 * MINUTE },
  { label: "6h", value: "6h", ms: 6 * HOUR, step: 5 * MINUTE, per: "5 minutes", tick: HOUR },
  { label: "24h", value: "24h", ms: DAY, step: 15 * MINUTE, per: "15 minutes", tick: 4 * HOUR },
  { label: "7d", value: "7d", ms: 7 * DAY, step: HOUR, per: "hour", tick: DAY },
  { label: "30d", value: "30d", ms: 30 * DAY, step: 6 * HOUR, per: "6 hours", tick: 5 * DAY },
  { label: "90d", value: "90d", ms: 90 * DAY, step: DAY, per: "day", tick: 15 * DAY },
];
const CLASS_TONES: Record<string, LedTone> = { "2": "up", "3": "off", "4": "unknown", "5": "down" };

/** The Metrics page: requests in a range, split by site, status and country, and one picked value over time. */
export function useMetrics() {
  const api = useApi();
  const range = ref<Range>("24h");
  const picked = ref<RankedRow>();
  const unavailable = ref(false);
  const failed = ref(false);
  const now = ref(Date.now());

  const { data, refresh } = useAsyncData("metrics", async () => {
    const series = (group: MetricsGroup) => api.GET("/api/v1/metrics/series", { params: { query: { range: range.value, group } } });
    const [route, status, country, routes] = await Promise.all([series("route"), series("status"), series("country"), api.GET("/api/v1/routes")]);
    unavailable.value = route.response.status === 503;
    failed.value = !route.data && !unavailable.value;
    now.value = Date.now();
    return { route: route.data ?? [], status: status.data ?? [], country: country.data ?? [], routes: routes.data ?? [] };
  }, { watch: [range] });

  const chosen = computed(() => RANGES.find((item) => item.value === range.value) as (typeof RANGES)[number]);
  // The first bucket starts before the range and holds only part of its requests, so it would read as a dip.
  const buckets = (points: SeriesPoint[]) => bucketSeries(points, now.value - chosen.value.ms, now.value, chosen.value.step).slice(1);

  const total = computed(() => buckets(data.value?.route ?? []));
  const pickedSeries = computed(() => {
    const row = picked.value;
    if (!row) return undefined;
    return buckets((data.value?.[row.group] ?? []).filter((point) => (point.group === null ? null : String(point.group)) === row.value));
  });

  const siteLabel = (name: string | null) => {
    const route = data.value?.routes.find((item) => item.name === name);
    return name === null ? "No site" : route ? routeTarget(route) : `${name} (removed)`;
  };
  const rows = (group: MetricsGroup, label: (value: string | null) => string): RankedRow[] =>
    totalsByGroup(data.value?.[group] ?? []).map(({ group: value, requests }) => ({
      group, value, label: label(value), requests, ...(group === "status" && { tone: CLASS_TONES[value?.[0] ?? ""] ?? "off" }),
    }));
  const lists = computed(() => ({
    route: rows("route", siteLabel),
    status: rows("status", (value) => value ?? "No status (TCP)"),
    country: rows("country", (value) => (value ? countryName(value) : "Unknown")),
  }));

  const requests = computed(() => lists.value.route.reduce((sum, row) => sum + row.requests, 0));
  const classes = computed(() => statusClasses(data.value?.status ?? []));

  // Requests arrive all the time, so the page refreshes on its own.
  const timer = setInterval(() => void (document.hidden || refresh()), 60_000);
  onBeforeUnmount(() => clearInterval(timer));

  return { range, picked, unavailable, failed, loaded: computed(() => Boolean(data.value)), total, pickedSeries, chosen, lists, requests, classes };
}
