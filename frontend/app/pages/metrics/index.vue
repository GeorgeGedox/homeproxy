<script setup lang="ts">
import type { RankedRow } from "~/composables/useMetrics";

const metrics = useMetrics();
const lists = metrics.lists;
const WORDS = { route: "to", status: "with status", country: "from" } as const;
// Rows with no value read badly after the usual words ("Requests to No site").
const NO_VALUE = { route: "Requests that matched no site", status: "Requests with no status (TCP)", country: "Requests from no known country" } as const;
const title = computed(() => {
  const row = metrics.picked.value;
  if (!row) return "Requests";
  return row.value === null ? NO_VALUE[row.group] : `Requests ${WORDS[row.group]} ${row.label}`;
});
const share = (count: number) => {
  if (!count || !metrics.requests.value) return "";
  const percent = (count / metrics.requests.value) * 100;
  return percent < 1 ? "<1%" : `${Math.round(percent)}%`;
};
const numbers = computed(() => [
  { label: "Requests", value: metrics.requests.value, note: "" },
  { label: "Client errors (4xx)", value: metrics.classes.value["4xx"], note: share(metrics.classes.value["4xx"]) },
  { label: "Server errors (5xx)", value: metrics.classes.value["5xx"], note: share(metrics.classes.value["5xx"]), error: metrics.classes.value["5xx"] > 0 },
]);

function pick(row: RankedRow): void {
  const same = metrics.picked.value?.group === row.group && metrics.picked.value.value === row.value;
  metrics.picked.value = same ? undefined : row;
}
</script>

<template>
  <UDashboardPanel id="metrics">
    <template #header>
      <UDashboardNavbar title="Metrics">
        <template #right>
          <UTabs v-model="metrics.range.value" :items="RANGES" :content="false" size="xs" aria-label="Time range" />
        </template>
      </UDashboardNavbar>
    </template>
    <template #body>
      <p v-if="metrics.unavailable.value" class="rounded-lg border border-default px-4 py-3 text-sm text-error">
        Metrics are unavailable: the metrics file could not be opened. Routing is not affected.
      </p>
      <p v-else-if="metrics.failed.value" class="rounded-lg border border-default px-4 py-3 text-sm text-error">The metrics could not be loaded.</p>
      <template v-else-if="metrics.loaded.value">
        <dl class="grid shrink-0 grid-cols-3 gap-px overflow-hidden rounded-lg border border-default bg-(--ui-border)">
          <div v-for="item in numbers" :key="item.label" class="bg-default px-4 py-3">
            <dt class="text-xs text-muted">{{ item.label }}</dt>
            <dd class="mt-1 flex flex-wrap items-baseline gap-x-2">
              <span class="font-mono text-2xl tabular-nums" :class="item.error ? 'text-error' : 'text-highlighted'">{{ formatCount(item.value) }}</span>
              <span v-if="item.note" class="font-mono text-xs text-muted">{{ item.note }}</span>
            </dd>
          </div>
        </dl>

        <section class="rounded-lg border border-default p-4" aria-labelledby="chart-title">
          <div class="mb-4 flex flex-wrap items-start gap-2">
            <div class="min-w-0 flex-1 basis-48">
              <h2 id="chart-title" class="text-sm text-highlighted">{{ title }}</h2>
              <p class="flex flex-wrap items-center gap-x-3 text-xs text-muted">
                <span>Requests per {{ metrics.chosen.value.per }}</span>
                <template v-if="metrics.picked.value">
                  <span class="flex items-center gap-1.5"><span class="h-0.5 w-3 rounded-full bg-primary" />{{ metrics.picked.value.label }}</span>
                  <span class="flex items-center gap-1.5"><span class="h-px w-3 bg-accented" />All requests</span>
                </template>
              </p>
            </div>
            <UButton v-if="metrics.picked.value" label="Show all" icon="lucide:x" color="neutral" variant="outline" size="xs" @click="metrics.picked.value = undefined" />
          </div>
          <p v-if="!metrics.requests.value" class="py-16 text-center text-sm text-muted">No requests in this range</p>
          <MetricsChart v-else class="h-60" :total="metrics.total.value" :picked="metrics.pickedSeries.value" :per="metrics.chosen.value.per" :tick="metrics.chosen.value.tick" />
        </section>

        <div class="grid gap-4 lg:grid-cols-3">
          <MetricsRanked title="Sites" :rows="lists.route" :picked="metrics.picked.value" @pick="pick" />
          <MetricsRanked title="Status codes" :rows="lists.status" :picked="metrics.picked.value" @pick="pick" />
          <MetricsRanked title="Countries" :rows="lists.country" :picked="metrics.picked.value" @pick="pick" />
        </div>
      </template>
    </template>
  </UDashboardPanel>
</template>
