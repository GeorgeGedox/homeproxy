import type { components } from "~~/api/schema";
import type { RouteUser } from "~/utils/route";

type RouteSaved = components["schemas"]["RouteSaved"];

/** Where the editor starts: an existing route, a copy of one, or a new path on a site's host. */
interface EditorStart { name?: string; copy?: string; host?: string; path?: string }

/** State and actions of the route editor: load a route, validate the draft as it changes, and save it. */
export function useRouteEditor({ name: editName, copy: copyName, host, path }: EditorStart) {
  const api = useApi();
  const pending = usePending();
  const toast = useToast();
  const draft = ref<RouteDraft>(blankDraft());
  const users = ref<RouteUser[]>([]);
  const savedUsers = ref<string[]>([]);
  /** The route being edited. Set after a create too, so a retry updates it instead of creating it again. */
  const original = ref<Route>();
  /** Every route, to find the site a host joins. */
  const routes = ref<Route[]>([]);
  /** HAProxy's LAN address, which the internal DNS server answers by default. */
  const lanIp = ref("");
  /** Every domain's zone, and those whose DNS is on Cloudflare when the integration is on. */
  const zones = ref<string[]>([]);
  const cloudflareZones = ref<string[]>([]);
  const loading = ref(true);
  const loadError = ref("");
  const saveError = ref("");
  /** The API's error body for a draft it refused, which the form shows at its field. */
  const failure = ref<unknown>();
  const saving = ref(false);
  const problems = ref<{ errors: string[]; warnings: string[] }>({ errors: [], warnings: [] });
  let timer: ReturnType<typeof setTimeout> | undefined;
  /** Counts checks, so a slow one (each unreachable upstream takes seconds) never lands after a newer one. */
  let checks = 0;

  const snapshot = () => JSON.stringify([draft.value, users.value]);
  const clean = ref("");
  const dirty = computed(() => !loading.value && snapshot() !== clean.value);

  /** The other websites on the draft's hosts: its site, which shares the host settings with it. */
  const siteOthers = computed(() => {
    if (draft.value.type !== "http") return [];
    return routes.value.filter((route) => route.type === "http" && route.name !== original.value?.name && route.hosts.some((h) => draft.value.hosts.includes(h)));
  });
  const siteSettingsOf = (d: RouteDraft) => JSON.stringify([d.exposure, d.dns_target, d.cloudflare_dns, d.cloudflare_proxied]);
  /** Whether staging the draft changes the host settings of the other paths too. */
  const siteChanged = computed(() => {
    const other = siteOthers.value[0];
    return Boolean(other) && siteSettingsOf(routeToDraft(other as Route)) !== siteSettingsOf(draft.value);
  });
  // A path that joins a site starts with the site's host settings.
  watch(() => siteOthers.value[0], (route) => {
    if (!route) return;
    const { exposure, dns_target, cloudflare_dns, cloudflare_proxied } = routeToDraft(route);
    Object.assign(draft.value, { exposure, dns_target, cloudflare_dns, cloudflare_proxied });
  });
  const cloudflareAvailable = computed(() => {
    const zone = draft.value.hosts[0] && zoneOf(draft.value.hosts[0], zones.value);
    return Boolean(zone) && cloudflareZones.value.includes(zone as string);
  });

  // A pasted URL becomes host:port, and its scheme says whether the upstream speaks TLS.
  watch(() => draft.value.upstreams, (upstreams) => {
    const parsed = upstreams.map(parseUpstream);
    const tls = parsed.find((upstream) => upstream.tls !== undefined)?.tls;
    if (tls !== undefined && draft.value.type === "http") draft.value.tls_enabled = tls;
    if (parsed.some((upstream, index) => upstream.address !== upstreams[index])) draft.value.upstreams = parsed.map((upstream) => upstream.address);
  }, { deep: true });

  async function load(): Promise<void> {
    const name = editName ?? copyName;
    const [list, settings, domains, one] = await Promise.all([
      api.GET("/api/v1/routes"), api.GET("/api/v1/settings"), api.GET("/api/v1/domains"),
      name ? api.GET("/api/v1/routes/{name}", { params: { path: { name } } }) : undefined,
    ]);
    routes.value = list.data ?? [];
    readContext(settings.data, domains.data);
    if (one && !one.data) loadError.value = apiErrorMessage(one.error, "The route was not found.");
    else if (one?.data) await startFrom(one.data);
    if (host) Object.assign(draft.value, { hosts: [host], path_prefix: path ?? "" });
    // The site's host settings arrive through the watcher above; the snapshot must include them.
    await nextTick();
    clean.value = snapshot();
    loading.value = false;
  }

  /** Keep what the site section shows besides the draft: the LAN address and the Cloudflare zones. */
  function readContext(settings?: { network: { haproxy_lan_ip: string }; cloudflare: { enabled: boolean } }, domains?: { zone: string; cloudflare_off_reason?: string | null }[]): void {
    lanIp.value = settings?.network.haproxy_lan_ip ?? "";
    zones.value = (domains ?? []).map((domain) => domain.zone);
    const onCloudflare = (domains ?? []).filter((domain) => !domain.cloudflare_off_reason).map((domain) => domain.zone);
    cloudflareZones.value = settings?.cloudflare.enabled ? onCloudflare : [];
  }

  /** Edit the loaded route, or start a copy of it without its hosts. */
  async function startFrom(route: Route): Promise<void> {
    draft.value = routeToDraft(route);
    if (!editName) {
      draft.value.hosts = [];
      return;
    }
    original.value = route;
    await loadUsers(editName);
  }

  async function loadUsers(name: string): Promise<void> {
    const { data, error } = await api.GET("/api/v1/routes/{name}/basic-auth", { params: { path: { name } } });
    // Users left out of a save are removed, so the form must not open with the list missing.
    if (!data) loadError.value = apiErrorMessage(error, "The basic auth users could not be loaded.");
    savedUsers.value = data?.users ?? [];
    users.value = savedUsers.value.map((user) => ({ name: user, password: "", existing: true }));
  }

  async function validate(): Promise<void> {
    const check = ++checks;
    problems.value = { errors: [], warnings: [] };
    failure.value = undefined;
    if (!draftIsComplete(draft.value)) return;
    const { data, error } = await api.POST("/api/v1/routes/validate", {
      params: { query: { name: original.value?.name } },
      body: draftToRoute(draft.value, original.value),
    });
    if (check !== checks) return;
    if (data) problems.value = data;
    else failure.value = error;
  }

  // Users are not part of the check, so typing a password sends none.
  watch(draft, () => {
    clearTimeout(timer);
    if (!loading.value) timer = setTimeout(() => void validate(), 400);
  }, { deep: true });

  function usersChanged(): boolean {
    const names = users.value.map((user) => user.name);
    return users.value.some((user) => user.password) || JSON.stringify(names) !== JSON.stringify(savedUsers.value);
  }

  /** Save the users. Only an edit says so: they skip the pending changes, so nothing else tells the admin they need an apply. */
  async function saveUsers(name: string, notice: boolean): Promise<boolean> {
    const body = { users: Object.fromEntries(users.value.map((user) => [user.name, user.password || null])) };
    const { response, error } = await api.PUT("/api/v1/routes/{name}/basic-auth", { params: { path: { name } }, body });
    if (!response.ok) saveError.value = apiErrorMessage(error, "The users were not saved.");
    else if (notice) toast.add({ title: "Basic auth users saved", description: "They take effect at the next apply." });
    return response.ok;
  }

  /** Stage the route. Returns what the API saved, or nothing with `failure` set. */
  async function saveRoute(route: Route): Promise<RouteSaved | undefined> {
    const { data, error } = original.value
      ? await api.PUT("/api/v1/routes/{name}", { params: { path: { name: original.value.name } }, body: route })
      : await api.POST("/api/v1/routes", { body: route });
    if (!data) failure.value = error ?? {};
    return data;
  }

  /** Say what the save did besides staging the route: its warnings, and the other paths of the site it changed. */
  function announce(saved: { warnings: string[]; site_changed: string[] }): void {
    if (saved.warnings.length) toast.add(warningToast(saved.warnings));
    if (saved.site_changed.length) toast.add({ title: "Also updated", description: saved.site_changed.join(", ") });
  }

  /** Stage the route, then save its basic auth users. Returns whether both were saved. */
  async function save(): Promise<boolean> {
    // A live check still waiting would clear the save's field errors when it runs.
    clearTimeout(timer);
    saving.value = true;
    saveError.value = "";
    failure.value = undefined;
    const ok = await stage();
    saving.value = false;
    return ok;
  }

  async function stage(): Promise<boolean> {
    const route = draftToRoute(draft.value, original.value);
    const withUsers = route.type === "http" && route.auth?.type === "basic";
    if (withUsers && users.value.length === 0) {
      saveError.value = "Add at least one user for password protection.";
      return false;
    }
    const changed = usersChanged();
    const notice = Boolean(original.value) && changed;
    const saved = await saveRoute(route);
    if (!saved) return false;
    original.value = saved.route;
    const ok = !withUsers || !changed || (await saveUsers(saved.route.name, notice));
    await pending.refresh();
    if (!ok) return false;
    clean.value = snapshot();
    announce(saved);
    return true;
  }

  onMounted(() => void load());
  onBeforeUnmount(() => clearTimeout(timer));

  return { draft, users, original, siteOthers, siteChanged, lanIp, cloudflareAvailable, loading, loadError, saveError, failure, saving, problems, dirty, isNew: !editName, save };
}
