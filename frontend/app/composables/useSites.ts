import type { LedTone } from "~/utils/health";

export interface RouteState { tone: LedTone; text: string }

/** The Routes page: routes grouped into sites, their live health and staged state, and the list's quick switches. */
export function useSites() {
  const api = useApi();
  const pending = usePending();
  const toast = useToast();
  const { data: status, refresh: refreshStatus } = useStatus();
  // Two errors, since the reload after a refused action would clear the action's error.
  const loadError = ref("");
  const actionError = ref("");
  const { data, refresh } = useAsyncData("sites", async () => {
    const [routes, warnings, domains, settings] = await Promise.all([
      api.GET("/api/v1/routes"), api.GET("/api/v1/routes/warnings"), api.GET("/api/v1/domains"), api.GET("/api/v1/settings"),
    ]);
    loadError.value = routes.data ? "" : apiErrorMessage(routes.error, "The routes could not be loaded.");
    const cloudflareOn = settings.data?.cloudflare.enabled ?? false;
    return {
      routes: routes.data ?? [],
      warnings: warnings.data ?? [],
      zones: (domains.data ?? []).map((domain) => domain.zone),
      cloudflareZones: cloudflareOn ? (domains.data ?? []).filter((domain) => !domain.cloudflare_off_reason).map((domain) => domain.zone) : [],
    };
  });

  // Health is read live from HAProxy; the routes change only through this dashboard, or an apply or discard.
  const timer = setInterval(() => void (document.hidden || refreshStatus()), 10_000);
  onBeforeUnmount(() => clearInterval(timer));
  watch([pending.applies, pending.discards], () => void Promise.all([refresh(), refreshStatus()]));

  const groups = computed(() => groupByZone(groupSites(data.value?.routes ?? []), data.value?.zones ?? []));
  const staged = computed(() => new Set((pending.review.value?.changes ?? []).map((change) => change.path.match(/^routes\/(.+)\.yaml$/)?.[1])));
  const health = computed(() => new Map((status.value?.routes ?? []).map((row) => [row.name, row.status])));

  /** What the status light of a route says: staged and not live yet, switched off, or HAProxy's health. */
  function routeState(route: Route): RouteState {
    const live = health.value.get(route.name);
    if (!route.enabled) return { tone: "off", text: "Switched off" };
    if (live === undefined) return { tone: "staged", text: "Staged, goes live at the next apply" };
    return { tone: healthTone(live), text: healthText(live) };
  }

  /** The worst state of a site's routes, for the light on its header. */
  function siteState(site: Site): RouteState {
    const states = site.routes.map(routeState);
    const order: LedTone[] = ["down", "unknown", "staged", "up", "off"];
    return order.map((tone) => states.find((state) => state.tone === tone)).find(Boolean) ?? { tone: "off", text: "" };
  }

  /** Whether the site's Cloudflare DNS switch can do anything: Cloudflare is on and manages the site's domain. */
  const cloudflareFor = (group: SiteGroup) => data.value?.cloudflareZones.includes(group.name) ?? false;

  /** The backend's warnings for a site's hosts: why a host may not answer. */
  const siteWarnings = (site: Site) => (data.value?.warnings ?? []).filter((row) => site.hosts.includes(row.host));

  /** Show the outcome of one save, then reload what it changed. */
  async function saved(result: { data?: { warnings: string[] }; error?: unknown }, failed: string): Promise<void> {
    actionError.value = result.data ? "" : apiErrorMessage(result.error, failed);
    if (result.data?.warnings.length) toast.add(warningToast(result.data.warnings));
    await Promise.all([refresh(), pending.refresh()]);
  }

  const updateRoute = async (route: Route) =>
    saved(await api.PUT("/api/v1/routes/{name}", { params: { path: { name: route.name } }, body: route }), `The route ${routeTarget(route)} was not saved.`);
  const updateSite = async (site: Site, body: { exposure?: "lan" | "public"; cloudflare_dns?: boolean }) =>
    saved(await api.PUT("/api/v1/routes/hosts/{host}", { params: { path: { host: site.hosts[0] as string } }, body }), `The site ${site.hosts[0]} was not saved.`);

  // A tcp site has no host and only one route, so its switch stays a single-route save.
  const setExposure = (site: Site, isPublic: boolean) => {
    const exposure = isPublic ? "public" : "lan";
    return site.hosts.length ? updateSite(site, { exposure }) : updateRoute({ ...site.routes[0], exposure } as Route);
  };
  const setCloudflare = (site: Site, dns: boolean) => updateSite(site, { cloudflare_dns: dns });
  const setEnabled = (route: Route, enabled: boolean) => updateRoute({ ...route, enabled });

  async function remove(route: Route): Promise<void> {
    const { response, error: failure } = await api.DELETE("/api/v1/routes/{name}", { params: { path: { name: route.name } } });
    actionError.value = response.ok ? "" : apiErrorMessage(failure, "The route was not deleted.");
    await Promise.all([refresh(), pending.refresh()]);
  }

  return { groups, error: computed(() => loadError.value || actionError.value), loaded: computed(() => Boolean(data.value)), cloudflareFor, staged, routeState, siteState, siteWarnings, setExposure, setCloudflare, setEnabled, remove };
}
