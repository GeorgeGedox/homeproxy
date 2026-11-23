<script setup lang="ts">
import type { Bucket } from "~/utils/traffic";

defineProps<{ buckets: Bucket[]; requests: number; errors: number }>();
const FOUR_HOURS = 4 * 3_600_000;
</script>

<template>
  <section aria-labelledby="traffic-title" class="flex flex-col rounded-lg border border-default p-4">
    <div class="mb-4 flex flex-wrap items-end justify-between gap-4">
      <div>
        <h2 id="traffic-title" class="text-sm text-muted">Requests, last 24 hours</h2>
        <p class="mt-1 font-mono text-3xl text-highlighted tabular-nums">{{ formatCount(requests) }}</p>
      </div>
      <div class="flex items-center gap-4 text-xs text-muted">
        <span><span class="font-mono tabular-nums" :class="errors ? 'text-error' : 'text-highlighted'">{{ formatCount(errors) }}</span> server errors</span>
        <UButton to="/metrics" label="Metrics" trailing-icon="lucide:arrow-right" color="neutral" variant="ghost" size="xs" class="text-muted" />
      </div>
    </div>
    <!-- The chart fills the card, which is as tall as Route health beside it. -->
    <MetricsChart :total="buckets" per="15 minutes" :tick="FOUR_HOURS" class="min-h-56 flex-1" />
  </section>
</template>
