import type { Job } from "~/utils/jobs";

/** The Cloudflare page: the records HomeProxy keeps in each zone, DDNS, and the sync, adopt and remove actions. */
export function useCloudflare() {
  const api = useApi();
  const pending = usePending();
  const toast = useToast();
  // Two errors: a load error clears once the records load, and the reload after an action keeps its error.
  const loadError = ref("");
  const error = ref("");
  const adoptErrors = ref<Record<string, string>>({});
  const busy = ref(false);

  const { data, refresh } = useAsyncData("cloudflare", async () => {
    const [state, routes, domains, settings] = await Promise.all([
      api.GET("/api/v1/cloudflare/records"), api.GET("/api/v1/routes"), api.GET("/api/v1/domains"), api.GET("/api/v1/settings"),
    ]);
    loadError.value = state.data ? "" : apiErrorMessage(state.error, "The Cloudflare records could not be loaded.");
    return { state: state.data, routes: routes.data ?? [], domains: domains.data ?? [], interval: settings.data?.cloudflare.reconcile_interval ?? "" };
  });

  const state = computed(() => data.value?.state);
  const status = computed(() => (state.value ? cloudflareStatus(state.value) : undefined));
  const zones = computed(() => (state.value?.zones ?? []).map((zone) => ({
    zone: zone.zone,
    rows: zoneRows(zone),
    errors: state.value ? zoneErrors(state.value, zone.zone) : [],
    ddns: state.value?.ddns.find((ddns) => ddns.zone === zone.zone),
  })));
  /** Domains with no Cloudflare records, and why, while the integration is on. */
  const notOnCloudflare = computed(() => (state.value?.enabled ? (data.value?.domains ?? []).flatMap((domain) => domain.cloudflare_off_reason ? [domain.cloudflare_off_reason] : []) : []));
  /** Whether any domain's DNS is on Cloudflare, the only place site records can be removed from. */
  const hasCloudflareDomain = computed(() => (data.value?.domains ?? []).some((domain) => domain.dns_provider === "cloudflare"));
  /** The site that owns a host, for the link to its editor. */
  const siteOf = (host: string) => data.value?.routes.find((route) => route.type !== "tcp" && route.hosts.includes(host))?.name;

  /** The newest job of a kind, so an action can tell the job it started from an older one. */
  async function newestJob(kind: string): Promise<Job | undefined> {
    return (await api.GET("/api/v1/jobs")).data?.find((job) => job.kind === kind);
  }

  /** Run an action that queues a job of `kind`, wait for that job, then reload. Returns the finished job. */
  async function thenFollow(kind: string, action: () => Promise<boolean>): Promise<Job | undefined> {
    busy.value = true;
    try {
      const before = (await newestJob(kind))?.id;
      if (!(await action())) return undefined;
      const job = await newestJob(kind);
      return job && job.id !== before ? await waitForJob(api, job.id) : undefined;
    } finally {
      busy.value = false;
      await refresh();
    }
  }

  /** Sync the records with what is applied now, then follow the DDNS update it queues. */
  async function sync(): Promise<void> {
    const job = await thenFollow("cloudflare.ddns", async () => {
      const { response, error: failure } = await api.POST("/api/v1/cloudflare/reconcile");
      error.value = response.ok ? "" : apiErrorMessage(failure, "The sync failed.");
      return response.ok;
    });
    if (job?.state === "failed") error.value = `DDNS update failed: ${job.error ?? "unknown error"}`;
  }

  async function adopt(zone: string): Promise<void> {
    adoptErrors.value = { ...adoptErrors.value, [zone]: "" };
    const job = await thenFollow("cloudflare.ddns", async () => {
      const { response, error: failure } = await api.POST("/api/v1/cloudflare/ddns/{zone}/adopt", { params: { path: { zone } } });
      if (!response.ok) adoptErrors.value = { ...adoptErrors.value, [zone]: apiErrorMessage(failure, "The record was not adopted.") };
      return response.ok;
    });
    if (job?.state === "failed") adoptErrors.value = { ...adoptErrors.value, [zone]: `Adopted, but the DDNS update failed: ${job.error ?? "unknown error"}` };
  }

  /** Take over the hand-made record on a site's host, then sync so it follows the site at once. */
  async function adoptHost(host: string): Promise<void> {
    adoptErrors.value = { ...adoptErrors.value, [host]: "" };
    const { response, error: failure } = await api.POST("/api/v1/cloudflare/records/{host}/adopt", { params: { path: { host } } });
    if (response.ok) return sync();
    adoptErrors.value = { ...adoptErrors.value, [host]: apiErrorMessage(failure, "The record was not adopted.") };
  }

  /** Delete every site record HomeProxy made, and say how many went. */
  async function removeSiteRecords(): Promise<void> {
    busy.value = true;
    const { data: zones, error: failure } = await api.POST("/api/v1/cloudflare/purge");
    busy.value = false;
    await refresh();
    if (!zones) {
      error.value = apiErrorMessage(failure, "The records were not removed.");
      return;
    }
    // A failed delete is reported as "<host>: <error>".
    const errors = zones.flatMap((zone) => zone.errors ?? []);
    const failed = new Set(errors.map((message) => message.split(": ")[0]));
    const removed = zones.flatMap((zone) => zone.changes ?? []).filter((change) => !failed.has(change.name)).length;
    error.value = errors.join("; ");
    toast.add({ title: removed ? `Removed ${removed === 1 ? "1 site record" : `${removed} site records`}` : "No site records to remove", color: errors.length ? "warning" : "success" });
  }

  // An apply or a discard empties the staged edits. An apply also queues a sync: wait for it, then reload once.
  watch(() => pending.staged.value, async (now, before) => {
    if (!before || now) return;
    const job = await newestJob("cloudflare.reconcile");
    if (job?.state === "queued" || job?.state === "running") await waitForJob(api, job.id);
    await refresh();
  });

  return { state, status, zones, notOnCloudflare, hasCloudflareDomain, interval: computed(() => data.value?.interval ?? ""), siteOf, error: computed(() => loadError.value || error.value), adoptErrors, busy, loaded: computed(() => Boolean(data.value)), sync, adopt, adoptHost, removeSiteRecords };
}
