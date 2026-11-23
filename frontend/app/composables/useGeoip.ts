import type { Job } from "~/utils/jobs";

const PROVIDERS = { "dbip-lite": "DB-IP Lite", maxmind: "MaxMind GeoLite2" } as const;

/** The countries blocked on every public route, the GeoIP data behind them, and its refresh job. */
export function useGeoip() {
  const api = useApi();
  const pending = usePending();
  const { state, error, refresh: load } = useGeoipCountries();
  const listError = ref("");
  const refreshing = ref(false);
  const refreshError = ref("");

  async function save(countries: string[]): Promise<void> {
    // Shown at once, so a second pick before the reload builds on this one. A refusal reloads the real list.
    if (state.value) state.value.block_countries = countries;
    const { response, error: failure } = await api.PUT("/api/v1/geoip", { body: { block_countries: countries } });
    listError.value = response.ok ? "" : apiErrorMessage(failure, "The block list was not staged.");
    await Promise.all([load(), pending.refresh()]);
  }

  /** Wait for a refresh job, then show the new data or the job's error. */
  async function follow(job: Job): Promise<void> {
    refreshing.value = true;
    const done = await waitForJob(api, job.id);
    refreshing.value = false;
    refreshError.value = done?.state === "failed" ? (done.error ?? "unknown error") : "";
    await load();
  }

  async function refresh(): Promise<void> {
    const { data: job, error: failure } = await api.POST("/api/v1/geoip/refresh");
    if (job) return follow(job);
    refreshError.value = apiErrorMessage(failure, "The refresh was not started.");
  }

  /** A failed job older than the data in use is not the state any more. */
  const outdated = (job: Job) => Boolean(state.value?.data && state.value.data.updated_at > job.created_at);

  /** A refresh that is still running, or the last one that failed, shows as on a click. */
  async function showLastRefresh(): Promise<void> {
    const job = (await api.GET("/api/v1/jobs")).data?.find((item) => item.kind === "geoip.refresh");
    if (job?.state === "queued" || job?.state === "running") return follow(job);
    if (job?.state === "failed" && !outdated(job)) refreshError.value = job.error ?? "unknown error";
  }

  onMounted(() => void showLastRefresh());
  // A discard can drop a staged block list, which the next pick would otherwise stage again.
  watch(pending.discards, () => void load());

  const providerName = (provider: keyof typeof PROVIDERS) => PROVIDERS[provider];
  return { state, error, listError, refreshing, refreshError, providerName, save, refresh };
}
