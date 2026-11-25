<script setup lang="ts">
const { file, summary, error, busy, restore } = useBackupRestore();
const confirming = ref(false);
const restored = ref(false);

async function confirm(): Promise<void> {
  if (await restore()) restored.value = true;
  else confirming.value = false;
}
</script>

<template>
  <SettingsSection title="Restore backup" description="Replace everything on this install with a backup. HomeProxy checks the file before anything changes.">
    <UFormField label="Backup file">
      <UFileUpload
        v-model="file" accept=".gz,.tgz,application/gzip" icon="lucide:upload" layout="list" class="w-full"
        label="Drop a backup here, or click to choose it" description="A .tar.gz from Download backup"
        @reject="error = 'This is not a .tar.gz file. Choose the backup file.'"
      />
    </UFormField>
    <p v-if="busy && !summary" class="flex items-center gap-2 text-sm text-muted">
      <UIcon name="lucide:loader-circle" class="size-4 animate-spin" />Checking the backup…
    </p>
    <BackupSummary v-if="summary" :summary="summary" />
    <UAlert v-if="error" color="error" variant="subtle" :description="error" />
    <UButton v-if="summary" label="Restore backup" icon="lucide:archive-restore" color="error" variant="outline" @click="confirming = true" />
    <UModal
      v-model:open="confirming" :dismissible="!restored && !busy" :close="!restored && !busy"
      :title="restored ? 'Backup restored' : 'Restore this backup?'"
      :description="restored ? undefined : 'Everything on this install is replaced with the backup: routes, domains, certificates, settings and history. The password becomes the one in the backup, and everyone is signed out, including you.'"
    >
      <template #body>
        <BackupRestored v-if="restored" />
        <div v-else class="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <p class="text-sm text-muted">If you might want this install back, download a backup of it first.</p>
          <UButton to="/api/v1/backup" external download icon="lucide:download" label="Download backup" color="neutral" variant="outline" class="self-start sm:self-auto" />
        </div>
      </template>
      <template v-if="!restored" #footer>
        <UButton label="Restore backup" color="error" :loading="busy" @click="confirm" />
        <UButton label="Cancel" color="neutral" variant="ghost" :disabled="busy" @click="confirming = false" />
      </template>
    </UModal>
  </SettingsSection>
</template>
