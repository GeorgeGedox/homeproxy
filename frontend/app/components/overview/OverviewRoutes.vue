<script setup lang="ts">
import type { RouteHealthRow } from "~/composables/useOverview";

defineProps<{ rows: RouteHealthRow[] }>();
</script>

<template>
  <section aria-labelledby="routes-title" class="flex flex-col rounded-lg border border-default">
    <h2 id="routes-title" class="flex items-center justify-between px-4 pt-3 pb-2 text-sm text-highlighted">
      Route health
      <UButton to="/routes" label="All routes" color="neutral" variant="ghost" size="xs" class="-my-1 text-muted" />
    </h2>
    <p v-if="!rows.length" class="border-t border-default px-4 py-6 text-sm text-muted">No live routes yet.</p>
    <!-- Capped, so a long list scrolls here instead of stretching the requests chart beside it. -->
    <ul v-else class="max-h-96 divide-y divide-default overflow-y-auto border-t border-default">
      <li v-for="row in rows" :key="row.name" class="flex items-center gap-3 px-4 py-2.5 text-sm">
        <UTooltip :text="healthText(row.state)">
          <StatusLed :tone="row.tone" :label="healthText(row.state)" />
        </UTooltip>
        <span class="min-w-0 flex-1 truncate font-mono text-highlighted">{{ row.label }}</span>
        <span v-if="row.requests !== null" class="font-mono text-xs text-dimmed tabular-nums">
          {{ formatCount(row.requests) }} req<template v-if="row.errors"> · <span class="text-error">{{ formatCount(row.errors) }} 5xx</span></template>
        </span>
      </li>
    </ul>
  </section>
</template>
