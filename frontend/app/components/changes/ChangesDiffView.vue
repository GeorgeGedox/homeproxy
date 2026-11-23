<script setup lang="ts">
const props = defineProps<{ diff: string }>();

const lines = computed(() => props.diff.split("\n"));

function tone(line: string): string {
  if (line.startsWith("+") && !line.startsWith("+++")) return "text-success";
  if (line.startsWith("-") && !line.startsWith("---")) return "text-error";
  return "text-muted";
}
</script>

<template>
  <section>
    <p v-if="!diff" class="text-muted">No differences.</p>
    <pre v-else class="overflow-x-auto rounded-md bg-muted p-3 font-mono text-xs"><code><span
      v-for="(line, i) in lines" :key="i" :class="['block', tone(line)]">{{ line || " " }}</span></code></pre>
  </section>
</template>
