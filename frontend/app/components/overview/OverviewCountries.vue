<script setup lang="ts">
const props = defineProps<{ countries: { group: string; requests: number }[] }>();

const top = computed(() => props.countries[0]?.requests ?? 0);
</script>

<template>
  <section aria-labelledby="countries-title" class="rounded-lg border border-default p-4">
    <h2 id="countries-title" class="text-sm text-highlighted">Top countries</h2>
    <p v-if="!countries.length" class="mt-3 text-sm text-muted">No public clients in the last 24 hours. LAN clients have no country.</p>
    <ul v-else class="mt-3 space-y-2">
      <li v-for="country in countries" :key="country.group" class="text-sm">
        <div class="flex items-center gap-2">
          <span :class="['fi shrink-0 rounded-[2px]', `fi-${country.group.toLowerCase()}`]" aria-hidden="true" />
          <span class="min-w-0 flex-1 truncate">{{ countryName(country.group) }}</span>
          <span class="font-mono text-highlighted tabular-nums">{{ formatCount(country.requests) }}</span>
        </div>
        <div class="mt-1 ml-8 h-1 rounded-full bg-accented">
          <div class="h-1 rounded-full bg-inverted/50" :style="{ width: `${(country.requests / top) * 100}%` }" />
        </div>
      </li>
    </ul>
  </section>
</template>
