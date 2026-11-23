import type { LedTone } from "~/utils/health";

/** The DNS page: whether the server answers, where it forwards, and every name it answers, live or staged. */
export function useDns() {
  const api = useApi();
  const pending = usePending();
  const toast = useToast();
  const { data: status, refresh: refreshStatus } = useStatus();
  // Two errors, since the reload after a refused remove would clear its error.
  const loadError = ref("");
  const removeError = ref("");
  const { data, refresh } = useAsyncData("dns", async () => {
    const [records, routes, settings] = await Promise.all([api.GET("/api/v1/dns/records"), api.GET("/api/v1/routes"), api.GET("/api/v1/settings")]);
    loadError.value = records.data ? "" : apiErrorMessage(records.error, "The DNS records could not be loaded.");
    return {
      served: records.data?.served ?? null,
      extra: records.data?.extra ?? [],
      routes: routes.data ?? [],
      lanIp: settings.data?.network.haproxy_lan_ip ?? "",
      server: settings.data?.dns_server,
    };
  });
  // An apply changes what the server answers; a discard drops staged records.
  watch([pending.applies, pending.discards], () => void Promise.all([refresh(), refreshStatus()]));

  const names = computed(() => dnsNames(data.value?.routes ?? [], data.value?.extra ?? [], data.value?.lanIp ?? "", data.value?.served?.records ?? {}));

  const stagedForwardTo = computed(() => data.value?.server?.forward_unknown_to ?? []);
  /** The resolvers the server forwards to now; before the first apply, the configured ones. */
  const forwardTo = computed(() => data.value?.served?.forward_to ?? stagedForwardTo.value);
  /** The resolvers the next apply sets, when they differ from the live ones. */
  const forwardStaged = computed(() => (stagedForwardTo.value.join() === forwardTo.value.join() ? null : stagedForwardTo.value));

  /** The server's light: answering, answering only its own names, not answering, or not started before the first apply. */
  const server = computed((): { tone: LedTone; text: string } => {
    if (!data.value?.served) return { tone: "off", text: "Not started yet" };
    if (!status.value?.dns_listening) return { tone: "down", text: "Not answering" };
    return forwardTo.value.length ? { tone: "up", text: "Answering" } : { tone: "unknown", text: "Answering, but only the names below" };
  });
  const listen = computed(() => data.value?.served?.listen ?? data.value?.server?.listen ?? "");
  const zones = computed(() => data.value?.served?.zones ?? []);
  const adguardLines = computed(() => zones.value.map((zone) => `[/${zone}/]${listen.value}`).join("\n"));

  async function copyAdguardLines(): Promise<void> {
    await navigator.clipboard.writeText(adguardLines.value);
    toast.add({ title: "Copied", description: `${zones.value.length === 1 ? "One line" : `${zones.value.length} lines`} for AdGuard Home` });
  }

  /** Stage an extra record. Returns the API's refusal, or undefined when it was staged. */
  async function add(name: string, target: string): Promise<unknown> {
    const { response, error: failure } = await api.POST("/api/v1/dns/records", { body: { name, target } });
    await Promise.all([refresh(), pending.refresh()]);
    return response.ok ? undefined : (failure ?? {});
  }

  async function remove(name: string): Promise<void> {
    const { response, error: failure } = await api.DELETE("/api/v1/dns/records/{name}", { params: { path: { name } } });
    removeError.value = response.ok ? "" : apiErrorMessage(failure, "The record was not removed.");
    await Promise.all([refresh(), pending.refresh()]);
  }

  return { names, server, listen, forwardTo, forwardStaged, zones, adguardLines, copyAdguardLines, error: computed(() => loadError.value || removeError.value), loaded: computed(() => Boolean(data.value)), add, remove };
}
