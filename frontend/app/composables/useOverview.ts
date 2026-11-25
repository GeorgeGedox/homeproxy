import type { components } from "~~/api/schema";
import type { LedTone } from "~/utils/health";
import type { Job } from "~/utils/jobs";

type Status = components["schemas"]["Status"];

export interface Attention { tone: LedTone; text: string; to?: string }
export interface RouteHealthRow { name: string; label: string; tone: LedTone; state: string; requests: number | null; errors: number | null }

const DAY = 86_400_000;
const BUCKET = 15 * 60_000;

/** Where a failed background job is fixed, and what to call it. */
function jobTarget(kind: string): { text: string; to?: string } {
  if (kind.startsWith("certificate.")) return { text: "A certificate job", to: "/domains" };
  if (kind === "cloudflare.ddns") return { text: "The DDNS update", to: "/cloudflare" };
  if (kind.startsWith("cloudflare.")) return { text: "The Cloudflare sync", to: "/cloudflare" };
  if (kind === "geoip.refresh") return { text: "The GeoIP refresh", to: "/security#countries" };
  return { text: `The ${kind} job` };
}

/** The newest run of each job and subject, kept only when it failed: a later success clears it. */
function failedJobs(jobs: Job[]): Attention[] {
  const latest = new Map<string, Job>();
  for (const job of jobs) {
    const key = `${job.kind}:${job.subject ?? ""}`;
    if (!latest.has(key)) latest.set(key, job);
  }
  return [...latest.values()].filter((job) => job.state === "failed").map((job) => {
    const target = jobTarget(job.kind);
    return { tone: "down", text: `${target.text}${job.subject ? ` for ${job.subject}` : ""} failed: ${job.error ?? "unknown error"}`, to: target.to };
  });
}

function statusProblems(status: Status): Attention[] {
  const problems: Attention[] = [];
  if (status.haproxy_error) problems.push({ tone: "down", text: `HAProxy does not answer: ${status.haproxy_error}` });
  const last = status.last_apply;
  if (last && !last.success) problems.push({ tone: "down", text: `The last apply failed at ${last.stage}: ${last.errors.join(" ") || "no detail"}`, to: "/history" });
  if (last?.success) problems.push(...last.warnings.map((warning): Attention => ({ tone: "unknown", text: warning })));
  if (!status.dns_listening) problems.push({ tone: "down", text: "The DNS server is not answering", to: "/dns" });
  if (status.bouncer === "down") problems.push({ tone: "down", text: "The CrowdSec bouncer is down. Traffic passes unchecked", to: "/security" });
  return problems;
}

/** Everything the Overview shows: status and traffic every 10 seconds, the slower lists and bans every minute. */
export function useOverview() {
  const api = useApi();
  const { data: status, refresh: refreshStatus } = useStatus();
  const { data, refresh: refreshData } = useAsyncData("overview", async () => {
    const [routes, domains, certificates, jobs] = await Promise.all([
      api.GET("/api/v1/routes"), api.GET("/api/v1/domains"), api.GET("/api/v1/certificates"), api.GET("/api/v1/jobs"),
    ]);
    // Undefined when they did not load, which `fresh` must not read as empty.
    return { routes: routes.data, domains: domains.data, certificates: certificates.data ?? [], jobs: jobs.data ?? [] };
  });
  const { data: metrics, refresh: refreshMetrics } = useAsyncData("overview-metrics", async () => {
    const series = (group?: "status" | "country") => api.GET("/api/v1/metrics/series", { params: { query: { range: "24h", group } } });
    const [total, byStatus, byCountry] = await Promise.all([series(), series("status"), series("country")]);
    return { total: total.data ?? [], byStatus: byStatus.data ?? [], byCountry: byCountry.data ?? [] };
  });

  // Counting reads the whole list from the LAPI, blocklists included, so it runs once a minute.
  const crowdsecOn = computed(() => Boolean(status.value && status.value.bouncer !== null));
  const { data: counts, refresh: refreshDecisions } = useAsyncData("overview-decisions", async () => (crowdsecOn.value ? (await api.GET("/api/v1/security/decisions/counts")).data ?? null : null), { watch: [crowdsecOn] });
  const bans = computed(() => (counts.value ? banSummary(counts.value) : undefined));

  // A hidden tab skips its polls; the next one after it shows again catches up.
  const timer = setInterval(() => void (document.hidden || Promise.all([refreshStatus(), refreshMetrics()])), 10_000);
  const slowTimer = setInterval(() => void (document.hidden || Promise.all([refreshData(), refreshDecisions()])), 60_000);
  onBeforeUnmount(() => {
    clearInterval(timer);
    clearInterval(slowTimer);
  });

  const fresh = computed(() => data.value?.routes?.length === 0 && data.value?.domains?.length === 0);

  const health = computed((): RouteHealthRow[] => (status.value?.routes ?? []).map((row) => {
    const route = data.value?.routes?.find((r) => r.name === row.name);
    return { name: row.name, label: route ? routeTarget(route) : row.name, tone: healthTone(row.status), state: row.status, requests: row.requests ?? null, errors: row.responses_5xx ?? null };
  }));

  const attention = computed((): Attention[] => [
    ...(status.value ? statusProblems(status.value) : []),
    ...health.value.filter((row) => row.tone === "down").map((row): Attention => ({ tone: "down", text: `${row.label} is down: HAProxy cannot reach its upstream`, to: "/routes" })),
    ...(data.value?.certificates ?? []).filter((cert) => cert.state !== "ok").map((cert): Attention => ({
      tone: cert.state === "alert" ? "down" : "unknown", text: `Certificate ${cert.name}: ${daysLeft(cert.days_left)}`, to: "/domains",
    })),
    ...(status.value?.cloudflare_unmarked ?? []).map((name): Attention => {
      const route = data.value?.routes?.find((r) => r.name === name);
      return { tone: "unknown", text: `${route ? routeTarget(route) : name} ${cloudflareUnmarkedAdvice}`, to: `/routes/edit?name=${encodeURIComponent(name)}` };
    }),
    ...failedJobs(data.value?.jobs ?? []),
  ]);

  const traffic = computed(() => {
    const end = Date.now();
    return bucketSeries(metrics.value?.total ?? [], end - DAY, end, BUCKET);
  });
  const requests = computed(() => (metrics.value?.total ?? []).reduce((sum, point) => sum + point.requests, 0));
  const classes = computed(() => statusClasses(metrics.value?.byStatus ?? []));
  const countries = computed(() => totalsByGroup(metrics.value?.byCountry ?? []).filter((row): row is { group: string; requests: number } => row.group !== null && row.group !== "LAN").slice(0, 5));

  return { status, data, fresh, health, attention, traffic, requests, classes, countries, bans };
}
