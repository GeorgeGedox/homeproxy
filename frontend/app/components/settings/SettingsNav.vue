<script setup lang="ts">
import type { NavigationMenuItem } from "@nuxt/ui";

/** `current` is a section id, or "dashboard" for the Dashboard and password page. */
const props = defineProps<{ current: string; changed?: readonly string[] }>();

const sections = SETTINGS_SECTIONS.map(({ label, id }) => ({ label, value: id as string, to: `/settings#${id}` }));
const dashboard = { label: "Dashboard and password", value: "dashboard", to: "/settings/dashboard" };
const backup = { label: "Backup", value: "backup", to: "/settings/backup" };
const entries = [...sections, dashboard, backup];
const item = (entry: (typeof entries)[number]): NavigationMenuItem => ({
  label: entry.label, to: entry.to, active: entry.value === props.current,
  slot: props.changed?.includes(entry.value) ? "changed" : undefined,
});
// Two groups: the staged sections, then the pages saved at once.
const items = computed(() => [sections.map(item), [item(dashboard), item(backup)]]);
const choice = computed({
  get: () => props.current,
  set: (value: string) => void navigateTo(entries.find((entry) => entry.value === value)?.to),
});
</script>

<template>
  <div class="md:sticky md:top-0 md:w-48 md:shrink-0 md:self-start">
    <UNavigationMenu
      :items="items" orientation="vertical" color="neutral" aria-label="Settings sections" class="hidden md:flex"
      :ui="{ separator: 'my-2', link: 'rounded-md' }"
    >
      <template #changed-trailing>
        <StatusLed tone="staged" label="Changed" />
      </template>
    </UNavigationMenu>
    <USelect v-model="choice" :items="entries" value-key="value" aria-label="Settings section" class="w-full md:hidden" />
  </div>
</template>
