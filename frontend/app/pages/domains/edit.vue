<script setup lang="ts">
import type { Domain } from "~/composables/useDomains";

const query = useRoute().query;
const zone = typeof query.zone === "string" ? query.zone : undefined;
const api = useApi();
const domain = ref<Domain>();
const loadError = ref("");
const loading = ref(Boolean(zone));
const dirty = ref(false);
const guard = useUnsavedGuard(dirty);

onMounted(async () => {
  if (!zone) return;
  const { data, error } = await api.GET("/api/v1/domains/{zone}", { params: { path: { zone } } });
  domain.value = data;
  loadError.value = data ? "" : apiErrorMessage(error, `${zone} could not be loaded.`);
  loading.value = false;
});
</script>

<template>
  <UDashboardPanel id="domain-edit">
    <template #header>
      <UDashboardNavbar :title="zone ? `Edit ${zone}` : 'Add domain'">
        <template #leading>
          <UButton to="/domains" icon="lucide:arrow-left" color="neutral" variant="ghost" size="sm" aria-label="Back to Domains" />
        </template>
      </UDashboardNavbar>
    </template>
    <template #body>
      <div v-if="loadError" class="max-w-2xl space-y-4">
        <UAlert color="error" variant="subtle" :description="loadError" />
        <UButton to="/domains" color="neutral" variant="subtle" label="Back to Domains" />
      </div>
      <DomainsForm v-else-if="!loading" v-model:dirty="dirty" :domain="domain" @saved="navigateTo('/domains')" />
      <UModal v-model:open="guard.open.value" :title="zone ? `Discard your edits to ${zone}?` : 'Discard your edits?'">
        <template #footer>
          <UButton color="error" variant="soft" label="Discard edits" @click="guard.discard" />
          <UButton color="neutral" variant="ghost" label="Keep editing" @click="guard.open.value = false" />
        </template>
      </UModal>
    </template>
  </UDashboardPanel>
</template>
