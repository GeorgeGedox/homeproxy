<script setup lang="ts">
import type { components } from "~~/api/schema";

/** The check answer of a valid backup. */
const props = defineProps<{ summary: components["schemas"]["BackupCheck"] }>();

const setLabel = (set: boolean) => (set ? "Set" : "Not set");
const rows = computed(() => [
  { label: "Routes", value: String(props.summary.routes) },
  { label: "Domains", value: props.summary.domains.join(", ") || "None" },
  { label: "Certificates", value: String(props.summary.certificates) },
  { label: "Snapshots", value: String(props.summary.snapshots) },
  { label: "Password", value: setLabel(props.summary.password_set) },
  { label: "Backup token", value: setLabel(props.summary.backup_token_set) },
  {
    label: "File version",
    value: `${props.summary.config_version}${props.summary.migrated ? " · from an older HomeProxy, updated on restore" : ""}`,
  },
]);
</script>

<template>
  <section class="rounded-md border border-default text-sm" aria-label="Backup check" data-testid="backup-summary">
    <header class="flex flex-wrap items-center justify-between gap-x-3 gap-y-1 rounded-t-md border-b border-default bg-muted px-3 py-2">
      <p class="inline-flex items-center gap-2 text-highlighted"><StatusLed tone="up" />Valid HomeProxy backup</p>
      <p v-if="summary.made_at" class="text-xs text-muted">made {{ dateTime(summary.made_at) }}</p>
    </header>
    <dl class="divide-y divide-default">
      <div v-for="row in rows" :key="row.label" class="flex justify-between gap-4 px-3 py-1.5">
        <dt class="shrink-0 text-muted">{{ row.label }}</dt>
        <dd class="min-w-0 text-right break-words text-highlighted">{{ row.value }}</dd>
      </div>
    </dl>
  </section>
</template>
