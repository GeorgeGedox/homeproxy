<script setup lang="ts">
/** The dashboard's status mark: a small square, like a link light on a switch port. */
import type { LedTone } from "~/utils/health";

const props = defineProps<{ tone: LedTone; label?: string }>();

const color: Record<LedTone, string> = {
  up: "bg-success",
  down: "bg-error",
  unknown: "bg-warning",
  staged: "bg-primary",
  off: "bg-accented",
};
</script>

<template>
  <span
    :class="['inline-block size-2 shrink-0 rounded-[2px]', color[props.tone]]"
    :role="label ? 'img' : undefined" :aria-label="label" :aria-hidden="label ? undefined : 'true'"
  />
</template>
