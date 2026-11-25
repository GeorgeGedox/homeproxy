<script setup lang="ts">
/** Two lists: every country on the left, the blocked ones on the right. A click moves a country across. */
const blocked = defineModel<string[]>({ required: true });
/** The list stands alone (the global list itself, or a route list that replaces it), so globally blocked countries are not left out. */
const props = defineProps<{ replaces?: boolean }>();
const geoip = useGeoipCountries();
const { countries, loaded, hasData, error } = geoip;
const global = computed(() => (props.replaces ? [] : geoip.global.value));
const search = ref("");

const available = computed(() => {
  const term = search.value.trim().toLowerCase();
  return countries.value.filter(({ code, name }) =>
    !blocked.value.includes(code) && !global.value.includes(code) && (!term || name.toLowerCase().includes(term) || code.toLowerCase() === term));
});
const chosen = computed(() => blocked.value.map((code) => ({ code, name: countryName(code) })).sort((a, b) => a.name.localeCompare(b.name)));

const block = (code: string) => (blocked.value = [...blocked.value, code]);
const unblock = (code: string) => (blocked.value = blocked.value.filter((item) => item !== code));
</script>

<template>
  <p v-if="error" class="text-sm text-error">{{ error }}</p>
  <div v-else-if="loaded && !hasData" class="text-sm text-muted">
    No country list yet. Download the GeoIP data on <ULink to="/security#countries" class="text-highlighted underline">Security, under Countries</ULink> first.
  </div>
  <div v-else class="space-y-2">
    <div class="grid gap-2 sm:grid-cols-2">
      <div class="overflow-hidden rounded-md border border-default">
        <UInput v-model="search" icon="lucide:search" placeholder="Find a country" variant="none" class="w-full border-b border-default" aria-label="Find a country" />
        <ul class="h-56 overflow-y-auto py-1" aria-label="Countries not blocked">
          <li v-for="country in available" :key="country.code">
            <button
              type="button" class="flex w-full items-center gap-3 px-3 py-1.5 text-left text-sm text-default transition-colors hover:bg-elevated"
              :aria-label="`Block ${country.name}`" @click="block(country.code)"
            >
              <span class="w-6 font-mono text-xs text-dimmed">{{ country.code }}</span>
              <span class="min-w-0 flex-1 truncate">{{ country.name }}</span>
              <UIcon name="lucide:plus" class="size-4 text-dimmed" />
            </button>
          </li>
          <li v-if="!available.length" class="px-3 py-1.5 text-sm text-dimmed">No country matches</li>
        </ul>
      </div>
      <div class="overflow-hidden rounded-md border border-default">
        <p class="border-b border-default px-3 py-2 text-sm text-muted">Blocked <span class="font-mono text-dimmed">{{ chosen.length }}</span></p>
        <ul class="h-56 overflow-y-auto py-1" aria-label="Blocked countries">
          <li v-for="country in chosen" :key="country.code">
            <button
              type="button" class="flex w-full items-center gap-3 px-3 py-1.5 text-left text-sm text-highlighted transition-colors hover:bg-elevated"
              :aria-label="`Unblock ${country.name}`" @click="unblock(country.code)"
            >
              <span class="w-6 font-mono text-xs text-dimmed">{{ country.code }}</span>
              <span class="min-w-0 flex-1 truncate">{{ country.name }}</span>
              <UIcon name="lucide:x" class="size-4 text-dimmed" />
            </button>
          </li>
          <li v-if="!chosen.length" class="px-3 py-1.5 text-sm text-dimmed">Pick countries on the left</li>
        </ul>
      </div>
    </div>
    <p v-if="global.length" class="text-xs text-dimmed">
      Blocked on every public route already: {{ global.map(countryName).join(", ") }}
    </p>
  </div>
</template>
