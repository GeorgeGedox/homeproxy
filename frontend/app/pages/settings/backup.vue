<template>
  <UDashboardPanel id="settings-backup">
    <template #header>
      <UDashboardNavbar title="Settings" />
    </template>
    <template #body>
      <div class="flex flex-col gap-4 md:flex-row md:gap-8">
        <SettingsNav current="backup" />
        <div class="min-w-0 max-w-3xl flex-1 space-y-4">
          <SettingsBackup />
          <SettingsBackupToken />
          <SettingsBackupRestore />
        </div>
      </div>
    </template>
  </UDashboardPanel>
</template>
