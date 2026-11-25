<script setup lang="ts">
/** The setup token, shared with the password form. */
const token = defineModel<string>("token", { required: true });
const { file, error, busy, restored, submit } = useSetupRestore(token);
</script>

<template>
  <section class="space-y-4 border-t border-default pt-4" aria-label="Restore from backup">
    <div class="space-y-1">
      <h2 class="text-sm text-highlighted">Restore from backup</h2>
      <p class="text-sm text-muted">Have a backup of another HomeProxy? Use it in place of the password above. It brings the config, the password and the certificates.</p>
    </div>
    <UAlert
      v-if="restored" color="success" variant="subtle" title="The backup is restored"
      description="The dashboard restarts now and uses the certificate from the backup. Reload this page in a few seconds and accept the certificate warning again. Then log in with the password from the backup."
    />
    <form v-else class="space-y-4" @submit.prevent="submit">
      <UFormField label="Backup file" description="A .tar.gz from Download backup, or from the tar command in the docs. Enter the setup token above.">
        <UInput type="file" accept=".gz,.tgz,application/gzip" class="w-full" @change="file = ($event.target as HTMLInputElement).files?.[0] ?? null" />
      </UFormField>
      <UAlert v-if="error" color="error" variant="subtle" :description="error" />
      <UButton type="submit" block color="neutral" variant="outline" :loading="busy" :disabled="!token" label="Restore backup" />
    </form>
  </section>
</template>
