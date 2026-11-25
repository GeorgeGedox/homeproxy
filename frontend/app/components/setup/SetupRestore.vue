<script setup lang="ts">
/** The setup token from the field above both ways to finish setup. */
const props = defineProps<{ token: string }>();
const emit = defineEmits<{ restored: [] }>();
const { file, error, busy, submit } = useSetupRestore(toRef(props, "token"));

async function restore(): Promise<void> {
  if (await submit()) emit("restored");
}
</script>

<template>
  <form class="space-y-4" @submit.prevent="restore">
    <p class="text-sm text-muted">Restore a backup of another HomeProxy. Its routes, password and certificates come with it.</p>
    <UFormField label="Backup file">
      <UFileUpload
        v-model="file" accept=".gz,.tgz,application/gzip" icon="lucide:download" layout="list" class="w-full"
        label="Drop the backup here, or click to choose it" description="The .tar.gz from Download backup, or from the tar command in the docs"
        @reject="error = 'This is not a .tar.gz file. Choose the backup file.'"
      />
    </UFormField>
    <UAlert v-if="error" color="error" variant="subtle" :description="error" />
    <UButton type="submit" block :loading="busy" :label="busy ? 'Restoring the backup…' : 'Restore backup'" />
  </form>
</template>
