<script setup lang="ts">
const cf = useCloudflare();
const pending = usePending();
const removeOpen = ref(false);

function remove(): void {
  removeOpen.value = false;
  void cf.removeSiteRecords();
}
</script>

<template>
  <UDashboardPanel id="cloudflare">
    <template #header>
      <UDashboardNavbar title="Cloudflare">
        <template #right>
          <UButton v-if="cf.state.value?.enabled && cf.zones.value.length" icon="lucide:refresh-cw" label="Sync now" :loading="cf.busy.value" @click="cf.sync" />
        </template>
      </UDashboardNavbar>
    </template>
    <template #body>
      <UAlert v-if="!cf.state.value && cf.error.value" color="error" variant="subtle" :description="cf.error.value" />
      <USkeleton v-if="!cf.loaded.value" class="h-16 w-full" />
      <template v-if="cf.state.value && cf.status.value">
        <CloudflareStatus
          :status="cf.status.value" :state="cf.state.value" :interval="cf.interval.value" :error="cf.error.value" :staged="pending.staged.value"
        />
        <template v-if="cf.state.value.enabled">
          <CloudflareZone
            v-for="zone in cf.zones.value" :key="zone.zone" v-bind="zone" :adopt-errors="cf.adoptErrors.value" :busy="cf.busy.value"
            :staged="pending.staged.value" :site-of="cf.siteOf" @adopt="cf.adopt(zone.zone)" @adopt-host="cf.adoptHost"
          />
          <UEmpty
            v-if="!cf.zones.value.length" icon="lucide:cloud" title="No domain has its DNS on Cloudflare"
            description="Set a domain's DNS provider to Cloudflare, and HomeProxy keeps a record for each public site you choose."
            :actions="[{ label: 'Open Domains', to: '/domains', color: 'neutral', variant: 'soft' }]"
          />
          <p v-if="cf.notOnCloudflare.value.length" class="text-xs text-muted">Not on Cloudflare: {{ cf.notOnCloudflare.value.join("; ") }}</p>
        </template>

        <section v-if="cf.hasCloudflareDomain.value" class="flex flex-col gap-3 rounded-lg border border-default px-4 py-3 sm:flex-row sm:items-center" aria-label="Remove site records">
          <div class="min-w-0 flex-1">
            <h2 class="text-sm text-highlighted">Remove site records</h2>
            <p class="text-xs text-muted">
              Deletes every record HomeProxy made for a site, in every Cloudflare zone. DDNS records and records you made stay.
              <template v-if="cf.state.value.enabled">Turn Cloudflare off in Settings first, or the next sync adds them again.</template>
            </p>
          </div>
          <UButton color="neutral" variant="outline" label="Remove site records" :disabled="cf.busy.value" class="self-start sm:self-auto" @click="removeOpen = true" />
        </section>
      </template>

      <UModal
        v-model:open="removeOpen" title="Remove every site record HomeProxy made?"
        :description="`They are deleted from Cloudflare now, in every zone. DDNS records and records you made stay.${cf.state.value?.enabled ? ' Cloudflare is still on, so the next sync adds them again.' : ''}`"
      >
        <template #footer>
          <UButton color="error" variant="soft" label="Remove site records" @click="remove" />
          <UButton color="neutral" variant="ghost" label="Keep them" @click="removeOpen = false" />
        </template>
      </UModal>
    </template>
  </UDashboardPanel>
</template>
