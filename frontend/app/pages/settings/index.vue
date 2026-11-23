<script setup lang="ts">
import type { components } from "~~/api/schema";

type Settings = components["schemas"]["Settings"];

const api = useApi();
const pending = usePending();
const { load: loadKeys } = useSettingsKeys();
const { load: loadUrls } = useNotificationUrls();
const form = ref<Settings>();
const loadError = ref("");
/** Bumped on each load, so the sections remount on the new form: some keep parts of it from their setup. */
const loads = ref(0);
/** The settings as last loaded or staged, to tell which sections changed. */
const saved = ref<Partial<Settings>>({});
const formRef = useTemplateRef("formRef");
const { error, show: showError, clear: clearError } = useFormError(formRef);
const warnings = ref<string[]>([]);
const busy = ref(false);

const snapshot = (settings: Settings | undefined): Partial<Settings> => JSON.parse(JSON.stringify(settings ?? {}));
const changed = computed(() => SETTINGS_SECTIONS.filter(({ key }) => JSON.stringify(form.value?.[key]) !== JSON.stringify(saved.value[key])));
const guard = useUnsavedGuard(computed(() => changed.value.length > 0));
// A link names the section in the hash, such as /settings#crowdsec.
const route = useRoute();
const hashed = () => SETTINGS_SECTIONS.find(({ id }) => route.hash === `#${id}`)?.id;
const tab = computed(() => hashed() ?? SETTINGS_SECTIONS[0].id);

/** Open the tab of the field a refusal names, so its error can be shown and scrolled to. */
async function openRefused(failure: unknown): Promise<void> {
  const { field } = apiFieldError(failure, "");
  const section = SETTINGS_SECTIONS.find(({ key }) => field?.startsWith(`${key}.`));
  if (!section) return;
  await navigateTo({ hash: `#${section.id}` }, { replace: true });
  await nextTick();
}

async function load(): Promise<void> {
  const { data, error: failure } = await api.GET("/api/v1/settings");
  loadError.value = data ? "" : apiErrorMessage(failure, "The settings could not be loaded.");
  form.value = data;
  loads.value += 1;
  // Sections fill defaults the API leaves out when they render, which is no change of the admin's.
  await nextTick();
  saved.value = snapshot(form.value);
}

onMounted(async () => {
  await load();
  await Promise.all([loadKeys(), loadUrls()]);
});
// A discard drops staged settings that this form still holds, and its next save would stage them again.
watch(pending.discards, () => void load());

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
</script>

<template>
  <UDashboardPanel id="settings">
    <template #header>
      <UDashboardNavbar title="Settings" />
    </template>
    <template #body>
      <div class="flex flex-col gap-4 md:flex-row md:gap-8">
        <SettingsNav :current="tab" :changed="changed.map((section) => section.id)" />
        <p v-if="loadError" class="text-sm text-error">{{ loadError }}</p>
        <UForm v-else-if="form" :key="loads" ref="formRef" :state="form" :validate-on="[]" class="min-w-0 max-w-3xl flex-1 space-y-4" @submit="save">
          <p class="text-sm text-muted">Changes here wait for Stage changes and then for Apply. Keys are the exception: they are saved at once.</p>
          <!-- v-show, not v-if: every section fills its defaults at mount, before the form is snapshot. -->
          <SettingsNetwork v-show="tab === 'network'" v-model="form.network" />
          <SettingsDns v-show="tab === 'dns'" v-model="form.dns_server" />
          <SettingsCertificates v-show="tab === 'certificates'" v-model="form.certificates" />
          <SettingsCloudflare v-show="tab === 'cloudflare'" v-model="form.cloudflare" />
          <SettingsCrowdsec v-show="tab === 'crowdsec'" v-model="form.crowdsec" />
          <SettingsGeoip v-show="tab === 'geoip'" v-model="form.geoip" />
          <SettingsNotifications v-show="tab === 'notifications'" v-model="form.notifications" />
          <SettingsPrometheus v-show="tab === 'prometheus'" v-model="form.prometheus" />

          <!-- The footer takes over the body's bottom room for the pending bar, so nothing scrolls below it. -->
          <div class="sticky -bottom-24 z-10 -mx-1 -mb-24 space-y-2 border-t border-default bg-default px-1 pt-3 pb-27 sm:-bottom-28 sm:-mb-28 sm:pb-31">
            <p v-if="error" class="text-sm text-error">{{ error }}</p>
            <p v-for="warning in warnings" :key="warning" class="flex items-start gap-2 text-sm text-muted">
              <UIcon name="lucide:triangle-alert" class="mt-0.5 size-4 shrink-0 text-warning" />{{ warning }}
            </p>
            <div class="flex flex-wrap items-center gap-3">
              <UButton type="submit" label="Stage changes" :color="changed.length ? 'primary' : 'neutral'" :loading="busy" :disabled="!changed.length" />
              <span class="text-xs text-muted">
                {{ changed.length ? `Changed: ${changed.map((section) => section.label).join(", ")}` : "No changes" }}
              </span>
            </div>
          </div>
        </UForm>
      </div>
      <UModal v-model:open="guard.open.value" title="Discard your settings edits?" :description="`Not staged yet: ${changed.map((section) => section.label).join(', ')}.`">
        <template #footer>
          <UButton color="error" variant="soft" label="Discard edits" @click="guard.discard" />
          <UButton color="neutral" variant="ghost" label="Keep editing" @click="guard.open.value = false" />
        </template>
      </UModal>
    </template>
  </UDashboardPanel>
</template>
