<script setup lang="ts">
const pending = usePending();
</script>

<template>
  <UDashboardPanel id="settings-dashboard">
    <template #header>
      <UDashboardNavbar title="Settings" />
    </template>
    <template #body>
      <div class="flex flex-col gap-4 md:flex-row md:gap-8">
      <SettingsNav current="dashboard" />
      <div class="min-w-0 max-w-3xl flex-1 space-y-4">
        <p class="text-sm text-muted">These are saved at once, never staged, so the dashboard keeps working whatever an apply does.</p>
        <p v-if="pending.staged.value" class="flex items-start gap-2 rounded-lg border border-default px-4 py-3 text-sm text-muted">
          <UIcon name="lucide:triangle-alert" class="mt-0.5 size-4 shrink-0 text-warning" />
          Staged changes hold a copy of the config, so the address and session length cannot be saved until you apply or discard them.
        </p>
        <SettingsDashboard :blocked="pending.staged.value" />
        <SettingsSection title="Password" description="The one admin password of this dashboard.">
          <UButton to="/settings/password" label="Change password" color="neutral" variant="outline" />
        </SettingsSection>
      </div>
      </div>
    </template>
  </UDashboardPanel>
</template>
