import type { components } from "~~/api/schema";

type View = components["schemas"]["DashboardView"];

/** The live dashboard settings: session length and listen address with its confirm-or-revert window. */
export function useDashboardSettings() {
  const api = useApi();
  const view = ref<View>();
  const form = reactive({ listen: "", session_ttl: "" });
  const listenError = ref("");
  const error = ref("");
  const saved = ref(false);
  const reverted = ref("");
  const busy = ref(false);
  const now = ref(Date.now());
  const timer = setInterval(() => (now.value = Date.now()), 1000);
  onScopeDispose(() => clearInterval(timer));

  const pendingListen = computed(() => view.value?.pending_listen ?? null);
  const secondsLeft = computed(() =>
    pendingListen.value ? Math.max(0, Math.ceil((new Date(pendingListen.value.expires_at).getTime() - now.value) / 1000)) : 0,
  );
  const listenChanged = computed(() => !!view.value && form.listen !== view.value.listen);

  async function load(): Promise<void> {
    const { data, error: failure } = await api.GET("/api/v1/settings/dashboard");
    if (!data) return void (error.value = apiErrorMessage(failure, "The dashboard settings could not be loaded."));
    view.value = data;
    Object.assign(form, { listen: data.listen, session_ttl: data.session_ttl });
  }

  async function save(): Promise<void> {
    busy.value = true;
    listenError.value = error.value = "";
    saved.value = false;
    reverted.value = "";
    const { data, error: failure } = await api.PUT("/api/v1/settings/dashboard", { body: { ...form } });
    busy.value = false;
    if (data) {
      view.value = data;
      saved.value = true;
      return;
    }
    const body = failure as { error?: { field?: string } } | undefined;
    const message = apiErrorMessage(failure, "The dashboard settings were not saved.");
    if (body?.error?.field === "listen") listenError.value = message;
    else error.value = message;
  }

  // The window ends on the server; reload so the page shows whether the change was kept.
  watch(secondsLeft, async (seconds) => {
    const wanted = pendingListen.value?.listen;
    if (seconds !== 0 || !wanted) return;
    await load();
    if (view.value?.listen === wanted) return;
    reverted.value = wanted;
    saved.value = false;
  });

  return { form, view, pendingListen, secondsLeft, listenChanged, listenError, error, saved, reverted, busy, load, save };
}
