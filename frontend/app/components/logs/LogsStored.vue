<script setup lang="ts">
import type { components } from "~~/api/schema";

const LAST = 200;
const props = defineProps<{ log: components["schemas"]["StoredLog"]; name: string }>();
const text = ref<string>();
const error = ref("");
const pre = useTemplateRef("pre");

/** The file is at most a few MB, so it is read whole and only its last lines are kept. */
async function load(): Promise<void> {
  const answer = await fetch(`/api/v1/logs/files/${props.log.source}`).catch(() => undefined);
  error.value = answer?.ok ? "" : "The log could not be read.";
  text.value = answer?.ok ? (await answer.text()).trimEnd().split("\n").slice(-LAST).join("\n") : "";
  // Open at the newest line, which is at the bottom.
  await nextTick();
  pre.value?.scrollTo({ top: pre.value.scrollHeight });
}
// The page keys this component by source, so a new log mounts a new one.
onMounted(load);
</script>

<template>
  <section class="rounded-lg border border-default" :aria-label="name">
    <header class="flex flex-wrap items-center gap-2 rounded-t-lg border-b border-default bg-muted px-4 py-3 text-xs text-muted">
      <span class="text-sm text-highlighted">{{ name }}</span>
      <span class="font-mono">{{ formatBytes(log.size) }}</span>
      <span v-if="log.modified">· changed {{ timeAgo(log.modified) }}</span>
      <span class="flex-1" />
      <UButton icon="lucide:refresh-cw" label="Reload" color="neutral" variant="ghost" size="sm" @click="load(); refreshNuxtData('log-files')" />
      <UButton :to="`/api/v1/logs/files/${log.source}`" external download label="Download" color="neutral" variant="outline" size="sm" />
    </header>
    <p v-if="error" class="px-4 py-3 text-sm text-error">{{ error }}</p>
    <p v-else-if="text === ''" class="px-4 py-6 text-center text-sm text-muted">Nothing logged yet</p>
    <template v-else-if="text">
      <pre ref="pre" class="max-h-[70vh] overflow-auto px-4 py-3 font-mono text-xs/5 break-words whitespace-pre-wrap text-default">{{ text }}</pre>
      <p class="border-t border-default px-4 py-2 text-xs text-muted">The last {{ LAST }} lines, newest at the bottom. Download for the whole log.</p>
    </template>
  </section>
</template>
