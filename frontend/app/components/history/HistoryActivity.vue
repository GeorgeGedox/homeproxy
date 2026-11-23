<script setup lang="ts">
const api = useApi();
const error = ref("");
const { data: entries } = useAsyncData("activity", async () => {
  const { data, error: failure } = await api.GET("/api/v1/audit", { params: { query: { limit: 500 } } });
  error.value = data ? "" : apiErrorMessage(failure, "The activity could not be loaded.");
  return (data ?? []).filter(isActivity);
});
</script>

<template>
  <UAlert v-if="error" color="error" variant="subtle" :description="error" />
  <section v-else-if="entries?.length" class="rounded-lg border border-default" aria-label="Activity">
    <ul class="divide-y divide-default">
      <li v-for="(entry, index) in entries" :key="`${entry.time}-${index}`" class="px-4 py-3">
        <p class="text-sm text-highlighted">{{ activityText(entry) }}</p>
        <p class="text-xs text-muted">{{ timeAgo(entry.time) }} · {{ dateTime(entry.time) }}</p>
      </li>
    </ul>
  </section>
  <UEmpty
    v-else-if="entries" icon="lucide:history" title="No activity yet"
    description="Changes saved at once, outside an apply, show here: credentials, keys, bans, always-allowed addresses and the dashboard settings."
  />
</template>
