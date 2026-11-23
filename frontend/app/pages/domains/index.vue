<script setup lang="ts">
import type { DomainEntry } from "~/composables/useDomains";

const list = useDomains();
const { providers, load: loadProviders } = useDnsProviders();
const toRemove = ref<DomainEntry>();
const removeOpen = computed({ get: () => Boolean(toRemove.value), set: () => (toRemove.value = undefined) });
const providerName = (code: string) => providers.value.find((p) => p.code === code)?.name ?? code;
onMounted(() => void loadProviders());

async function remove(): Promise<void> {
  const entry = toRemove.value;
  toRemove.value = undefined;
  if (entry) await list.remove(entry.domain);
}
</script>

<template>
  <UDashboardPanel id="domains">
    <template #header>
      <UDashboardNavbar title="Domains">
        <template #right>
          <UButton to="/domains/edit" icon="lucide:plus" label="Add domain" />
        </template>
      </UDashboardNavbar>
    </template>
    <template #body>
      <UAlert v-if="list.error.value" color="error" variant="subtle" :description="list.error.value" />
      <UEmpty
        v-if="list.loaded.value && !list.entries.value.length" icon="lucide:globe" title="No domains yet"
        description="A domain is the zone your sites live under, such as example.com. Add it first, then its sites."
        :actions="[{ label: 'Add domain', icon: 'lucide:plus', to: '/domains/edit' }]"
      />
      <DomainsCard
        v-for="entry in list.entries.value" :key="entry.domain.zone" :entry="entry" :cloudflare-on="list.cloudflareOn.value"
        :provider-name="providerName" @edit="navigateTo({ path: '/domains/edit', query: { zone: entry.domain.zone } })" @remove="toRemove = entry"
        @renew="list.renew"
      />
      <DomainsCredentialList
        v-if="list.entries.value.length || list.credentials.value.length" :credentials="list.credentials.value"
        :load-error="list.credentialsError.value" :providers="providers" @changed="list.reload"
      />
      <UModal v-model:open="removeOpen" :title="`Remove ${toRemove?.domain.zone}?`">
        <template #description>
          <template v-if="toRemove?.sites">
            {{ toRemove.sites === 1 ? "One site uses" : `${toRemove.sites} sites use` }} it. Remove or move them on Routes first.
          </template>
          <template v-else>It stops at the next apply, and its certificates are no longer renewed. Until then, Discard in the bar brings it back.</template>
        </template>
        <template #footer>
          <UButton v-if="toRemove?.sites" to="/routes" label="Open Routes" color="neutral" variant="soft" />
          <UButton v-else color="error" variant="soft" label="Remove domain" @click="remove" />
          <UButton color="neutral" variant="ghost" label="Keep it" @click="toRemove = undefined" />
        </template>
      </UModal>
    </template>
  </UDashboardPanel>
</template>
