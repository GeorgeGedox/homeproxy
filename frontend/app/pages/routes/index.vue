<script setup lang="ts">
const list = useSites();
const toDelete = ref<Route>();
const countOf = (group: SiteGroup) => `${group.sites.length} ${group.name === "TCP" ? "port" : "site"}${group.sites.length === 1 ? "" : "s"}`;
const confirmOpen = computed({ get: () => Boolean(toDelete.value), set: () => (toDelete.value = undefined) });

async function remove(): Promise<void> {
  const route = toDelete.value;
  toDelete.value = undefined;
  if (route) await list.remove(route);
}
</script>

<template>
  <UDashboardPanel id="routes">
    <template #header>
      <UDashboardNavbar title="Routes">
        <template #right>
          <UButton to="/routes/edit" icon="lucide:plus" label="Add site" />
        </template>
      </UDashboardNavbar>
    </template>
    <template #body>
      <UAlert v-if="list.error.value" color="error" variant="subtle" :description="list.error.value" />
      <UEmpty
        v-if="list.loaded.value && !list.groups.value.length" icon="lucide:route" title="No sites yet"
        description="A site sends a host name, or a path on it, to a service on your network."
        :actions="[{ label: 'Add site', icon: 'lucide:plus', to: '/routes/edit' }]"
      />
      <section v-for="group in list.groups.value" :key="group.name" class="space-y-3">
        <h2 class="flex items-baseline gap-2 font-mono text-sm text-muted">{{ group.name }}<span class="text-dimmed">{{ countOf(group) }}</span></h2>
        <RoutesSite
          v-for="site in group.sites" :key="site.key" :site="site"
          :state="list.siteState(site)" :route-state="list.routeState" :staged="list.staged.value" :cloudflare-on="list.cloudflareFor(group)"
          :warnings="list.siteWarnings(site)"
          @exposure="list.setExposure(site, $event)" @cloudflare="list.setCloudflare(site, $event)"
          @enabled="(route, on) => list.setEnabled(route, on)" @remove="toDelete = $event"
        />
      </section>
      <UModal
        v-model:open="confirmOpen" :title="`Delete ${toDelete ? routeTarget(toDelete) : ''}?`"
        description="It stops answering at the next apply. Until then, Discard in the bar brings it back."
      >
        <template #footer>
          <UButton color="error" variant="soft" label="Delete" @click="remove" />
          <UButton color="neutral" variant="ghost" label="Keep it" @click="toDelete = undefined" />
        </template>
      </UModal>
    </template>
  </UDashboardPanel>
</template>
