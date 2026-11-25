<script setup lang="ts">
import type { RankedRow } from "~/composables/useMetrics";

const TOP = 8;
const props = defineProps<{ title: string; rows: RankedRow[]; picked?: RankedRow }>();
defineEmits<{ pick: [RankedRow] }>();
const all = ref(false);
const shown = computed(() => (all.value ? props.rows : props.rows.slice(0, TOP)));
const peak = computed(() => Math.max(1, ...props.rows.map((row) => row.requests)));
const isPicked = (row: RankedRow) => props.picked?.group === row.group && props.picked.value === row.value;
</script>

<template>
  <section class="rounded-lg border border-default" :aria-label="title">
    <h2 class="rounded-t-lg border-b border-default bg-muted px-4 py-2 text-sm text-highlighted">{{ title }}</h2>
    <ul v-if="rows.length" class="py-1">
      <li v-for="row in shown" :key="String(row.value)">
        <button
          type="button" class="flex w-full items-center gap-3 px-4 py-1.5 text-left text-sm transition-colors hover:bg-elevated"
          :class="isPicked(row) ? 'bg-elevated text-highlighted' : 'text-default'" :aria-pressed="isPicked(row)" :aria-label="`Show ${row.label} over time`"
          @click="$emit('pick', row)"
        >
          <StatusLed v-if="row.tone" :tone="row.tone" />
          <span v-if="row.group === 'country' && row.value && row.value !== 'LAN'" :class="['fi shrink-0 rounded-[2px]', `fi-${row.value.toLowerCase()}`]" aria-hidden="true" />
          <span class="min-w-0 flex-1">
            <span class="block truncate" :class="row.group !== 'country' && row.value !== null && 'font-mono'">{{ row.label }}</span>
            <span class="mt-1 block h-1 rounded-full bg-elevated">
              <span class="block h-full rounded-full" :class="isPicked(row) ? 'bg-primary' : 'bg-accented'" :style="{ width: `${(row.requests / peak) * 100}%` }" />
            </span>
          </span>
          <span class="w-12 shrink-0 text-right font-mono text-xs text-muted tabular-nums">{{ formatCount(row.requests) }}</span>
        </button>
      </li>
    </ul>
    <p v-else class="px-4 py-3 text-sm text-muted">No requests</p>
    <div v-if="rows.length > TOP" class="border-t border-default px-4 py-1.5">
      <UButton :label="all ? 'Show fewer' : `Show all ${rows.length}`" color="neutral" variant="link" size="sm" class="px-0" @click="all = !all" />
    </div>
  </section>
</template>
