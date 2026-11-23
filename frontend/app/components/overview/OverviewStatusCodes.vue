<script setup lang="ts">
const props = defineProps<{ classes: Record<"2xx" | "3xx" | "4xx" | "5xx", number> }>();

// The share is one stacked bar; only errors get a color, everything else stays on the ramp.
const TONES = { "2xx": "bg-inverted/70", "3xx": "bg-inverted/35", "4xx": "bg-warning", "5xx": "bg-error" } as const;
const total = computed(() => Object.values(props.classes).reduce((a, b) => a + b, 0));
const rows = computed(() => (Object.keys(TONES) as (keyof typeof TONES)[]).map((key) => ({
  key, count: props.classes[key], share: total.value ? (props.classes[key] / total.value) * 100 : 0, tone: TONES[key],
})));
</script>

<template>
  <section aria-labelledby="codes-title" class="rounded-lg border border-default p-4">
    <h2 id="codes-title" class="text-sm text-highlighted">Responses</h2>
    <p v-if="!total" class="mt-3 text-sm text-muted">No requests in the last 24 hours.</p>
    <template v-else>
      <div class="mt-4 flex h-2 gap-px overflow-hidden rounded-sm bg-accented">
        <div v-for="row in rows.filter((r) => r.count)" :key="row.key" :class="row.tone" :style="{ width: `${row.share}%` }" />
      </div>
      <ul class="mt-4 grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
        <li v-for="row in rows" :key="row.key" class="flex items-center gap-2">
          <span :class="['size-2 rounded-[2px]', row.tone]" />
          <span class="font-mono text-muted">{{ row.key }}</span>
          <span class="ml-auto font-mono text-highlighted tabular-nums">{{ formatCount(row.count) }}</span>
        </li>
      </ul>
    </template>
  </section>
</template>
