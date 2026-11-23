<script setup lang="ts">
import type { Attention } from "~/composables/useOverview";

defineProps<{ items: Attention[] }>();
</script>

<template>
  <section aria-labelledby="attention-title" class="rounded-lg border border-default">
    <h2 id="attention-title" class="flex items-center justify-between px-4 pt-3 pb-2 text-sm text-highlighted">
      Needs attention
      <span class="font-mono text-xs text-dimmed tabular-nums">{{ items.length }}</span>
    </h2>
    <ul class="divide-y divide-default border-t border-default">
      <li v-for="item in items" :key="item.text">
        <ULink
          raw as="div" :to="item.to"
          class="flex items-center gap-3 px-4 py-2.5 text-sm" :class="item.to && 'group transition-colors [--focus-offset:-2px] hover:bg-elevated'"
        >
          <StatusLed :tone="item.tone" />
          <span class="line-clamp-3 min-w-0 flex-1 break-words" :title="item.text">{{ item.text }}</span>
          <UIcon v-if="item.to" name="lucide:arrow-right" class="size-4 shrink-0 text-dimmed group-hover:text-highlighted" />
        </ULink>
      </li>
    </ul>
  </section>
</template>
