import type { components } from "~~/api/schema";

type Settings = components["schemas"]["Settings"];

const snapshot = (settings: Settings | undefined): Partial<Settings> => JSON.parse(JSON.stringify(settings ?? {}));

/** The settings form: load the settings, tell which sections changed, and stage them. */
export function useSettingsForm(formRef: Parameters<typeof useFormError>[0]) {
  const api = useApi();
  const pending = usePending();
  const { load: loadUrls } = useNotificationUrls();
  const form = ref<Settings>();
  const loadError = ref("");
  /** Bumped on each load, so the sections remount on the new form: some keep parts of it from their setup. */
  const loads = ref(0);
  /** The settings as last loaded or staged, to tell which sections changed. */
  const saved = ref<Partial<Settings>>({});
  const { error, show: showError, clear: clearError } = useFormError(formRef);
  const warnings = ref<string[]>([]);
  const busy = ref(false);
  const changed = computed(() => SETTINGS_SECTIONS.filter(({ key }) => JSON.stringify(form.value?.[key]) !== JSON.stringify(saved.value[key])));

  /** Open the tab of the field a refusal names, so its error can be shown and scrolled to. */
  async function openRefused(failure: unknown): Promise<void> {
    const { field } = apiFieldError(failure, "");
    const section = SETTINGS_SECTIONS.find(({ key }) => field?.startsWith(`${key}.`));
    if (!section) return;
    await navigateTo({ hash: `#${section.id}` }, { replace: true });
    await nextTick();
  }

  /** Load the settings into the form. */
  async function load(): Promise<void> {
    const { data, error: failure } = await api.GET("/api/v1/settings");
    loadError.value = data ? "" : apiErrorMessage(failure, "The settings could not be loaded.");
    form.value = data;
    loads.value += 1;
    // Sections fill defaults the API leaves out when they render, which is no change of the admin's.
    await nextTick();
    saved.value = snapshot(form.value);
  }

  // A discard drops staged settings that this form still holds, and its next save would stage them again.
  watch(pending.discards, () => void load());

  /** Stage the form, or show why the API refused it. */
  async function save(): Promise<void> {
    if (!form.value) return;
    busy.value = true;
    clearError();
    warnings.value = [];
    const { data, response, error: failure } = await api.PUT("/api/v1/settings", { body: form.value });
    if (response.ok) {
      saved.value = snapshot(form.value);
      warnings.value = data?.warnings ?? [];
      await loadUrls();
    } else {
      await openRefused(failure);
      showError(failure, "The settings were not staged.");
    }
    await pending.refresh();
    busy.value = false;
  }

  return { form, loadError, loads, changed, error, warnings, busy, load, save };
}
