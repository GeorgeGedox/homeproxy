<script setup lang="ts">
import type { LedTone } from "~/utils/health";

const geoip = useGeoip();
const state = geoip.state;
const section = useTemplateRef("section");
const route = useRoute();
// The panel body scrolls, not the window, so the browser's own jump to #countries does nothing.
watch(state, () => route.hash === "#countries" && nextTick(() => section.value?.scrollIntoView()), { once: true });

const light = computed((): { tone: LedTone; text: string } => {
  if (geoip.refreshing.value) return { tone: "unknown", text: "Downloading…" };
  if (geoip.refreshError.value) return { tone: "down", text: "The last refresh failed" };
  if (!state.value?.data) return { tone: "off", text: "No data yet" };
  const count = state.value.block_countries.length;
  return { tone: count ? "up" : "off", text: count === 1 ? "1 country blocked" : `${count || "No"} countries blocked` };
});
const facts = computed(() => {
  const data = state.value?.data;
  return data ? `${geoip.providerName(data.provider)} · ${formatCount(data.entries)} ranges · updated ${timeAgo(data.updated_at)}` : "";
});
</script>

<template>
  <section id="countries" ref="section" class="scroll-mt-4 rounded-lg border border-default" aria-label="Countries">
    <header class="flex flex-wrap items-center gap-3 rounded-t-lg border-b border-default bg-muted px-4 py-3">
      <UTooltip :text="light.text"><StatusLed :tone="light.tone" :label="light.text" /></UTooltip>
      <div class="min-w-0 flex-1 basis-56">
        <h2 class="text-sm text-highlighted">Countries <span class="text-muted">· {{ light.text }}</span></h2>
        <p v-if="state" class="text-xs text-muted">
          <template v-if="facts">{{ facts }} · </template>
          <ULink :to="state.attribution.url" target="_blank" rel="noopener" class="text-muted underline hover:text-highlighted">{{ state.attribution.text }}</ULink>
        </p>
      </div>
      <UButton
        icon="lucide:refresh-cw" :label="geoip.refreshing.value ? 'Downloading…' : 'Refresh data'" color="neutral" variant="outline" size="sm"
        :loading="geoip.refreshing.value" @click="geoip.refresh"
      />
    </header>
    <p v-if="geoip.error.value || geoip.refreshError.value" class="border-b border-default px-4 py-2 text-sm text-error">
      {{ geoip.error.value || `The download failed: ${geoip.refreshError.value}` }}
    </p>
    <div v-if="state" class="space-y-2 px-4 py-3">
      <p class="text-sm text-muted">Blocked on every public site. A site can block more in its own settings. Changes take effect at the next apply.</p>
      <p v-if="!state.data" class="text-sm text-muted">
        Download the data first. It names the countries and their address ranges, and the provider is set in Settings.
      </p>
      <GeoipCountryPicker v-else :model-value="state.block_countries" every-route @update:model-value="geoip.save" />
      <p v-if="geoip.listError.value" class="text-sm text-error">{{ geoip.listError.value }}</p>
    </div>
  </section>
</template>
