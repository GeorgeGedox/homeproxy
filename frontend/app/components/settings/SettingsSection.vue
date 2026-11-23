<script setup lang="ts">
/** One card of settings. `id` is the anchor the section nav jumps to. */
defineProps<{ id?: string; title: string; description?: string }>();
</script>

<template>
  <section :id="id" class="scroll-mt-4 rounded-lg border border-default" :aria-label="title">
    <header class="rounded-t-lg border-b border-default bg-muted px-4 py-3">
      <h2 class="text-sm text-highlighted">{{ title }}</h2>
      <p v-if="description" class="text-xs text-muted">{{ description }}</p>
    </header>
    <div class="space-y-4 px-4 py-4"><slot /></div>
  </section>
</template>
