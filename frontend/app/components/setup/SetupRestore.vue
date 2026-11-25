<script setup lang="ts">
/** The setup token from the field above both ways to finish setup. */
const props = defineProps<{ token: string }>();
const emit = defineEmits<{ restored: [] }>();
const { file, summary, error, busy, check, restore } = useBackupRestore(toRef(props, "token"));
const label = computed(() => {
  if (summary.value) return busy.value ? "Restoring the backup…" : "Restore backup";
  return busy.value ? "Checking the backup…" : "Check backup";
});

// The file is checked when it is chosen. The button checks it again, for example after a wrong setup token.
async function submit(): Promise<void> {
  if (!summary.value) return check();
  if (await restore()) emit("restored");
}
</script>

<template>
  <form class="space-y-4" @submit.prevent="submit">
    <p class="text-sm text-muted">Restore a backup of another HomeProxy. Its routes, password and certificates come with it.</p>
    <UFormField label="Backup file">
      <UFileUpload
        v-model="file" accept=".gz,.tgz,application/gzip" icon="lucide:download" layout="list" class="w-full"
        label="Drop the backup here, or click to choose it" description="The .tar.gz from Download backup, or from the tar command in the docs"
        @reject="error = 'This is not a .tar.gz file. Choose the backup file.'"
      />
    </UFormField>
    <BackupSummary v-if="summary" :summary="summary" />
    <UAlert v-if="error" color="error" variant="subtle" :description="error" />
    <UButton type="submit" block :loading="busy" :label="label" />
  </form>
</template>
