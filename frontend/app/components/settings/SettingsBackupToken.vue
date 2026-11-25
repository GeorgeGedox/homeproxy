<script setup lang="ts">
const { status, token, error, busy, load, create, remove, copy } = useBackup();
onMounted(() => void load());

const confirming = ref<"replace" | "remove" | null>(null);
const confirmOpen = computed({ get: () => confirming.value !== null, set: (open: boolean) => void (open || (confirming.value = null)) });
const commands = computed(() => backupCommands(window.location.host));

async function confirm(): Promise<void> {
  const action = confirming.value;
  confirming.value = null;
  await (action === "remove" ? remove() : create());
}
</script>

<template>
  <SettingsSection title="Backup token" description="Lets a script download the backup, for example every night. It works for the backup download only.">
    <UAlert v-if="error" color="error" variant="subtle" :description="error" />
    <p v-if="status" class="text-sm">
      <template v-if="status.set">A backup token is set, made on {{ status.created_at ? new Date(status.created_at).toLocaleString() : "an unknown date" }}.</template>
      <template v-else>No backup token is set.</template>
    </p>
    <UAlert
      v-if="token"
      color="warning"
      variant="subtle"
      title="Copy the token now"
      description="It is shown only once. HomeProxy keeps only a hash of it."
    >
      <template #actions>
        <code class="rounded-md border border-default bg-muted px-3 py-2 font-mono text-xs break-all text-highlighted" data-testid="backup-token">{{ token }}</code>
        <UButton icon="lucide:copy" label="Copy token" color="neutral" variant="outline" @click="copy" />
      </template>
    </UAlert>
    <div class="flex gap-2">
      <UButton
        :label="status?.set ? 'Replace token' : 'Create token'" :loading="busy"
        @click="status?.set ? (confirming = 'replace') : create()"
      />
      <UButton v-if="status?.set" label="Remove token" color="error" variant="outline" @click="confirming = 'remove'" />
    </div>
    <div class="space-y-2">
      <p class="text-sm text-muted">
        A nightly script can use the token like this. Put the token in the variable <code>HOMEPROXY_BACKUP_TOKEN</code>.
        The dashboard certificate is self-signed, so the script saves it once and checks every download against it.
      </p>
      <pre class="overflow-x-auto rounded-md border border-default bg-muted px-3 py-2 font-mono text-xs text-highlighted">{{ commands }}</pre>
    </div>
    <UModal
      v-model:open="confirmOpen"
      :title="confirming === 'remove' ? 'Remove the backup token?' : 'Replace the backup token?'"
      :description="confirming === 'remove' ? 'A script that holds the token can no longer download the backup.' : 'The old token stops working at once. Update every script that holds it.'"
    >
      <template #footer>
        <UButton :label="confirming === 'remove' ? 'Remove token' : 'Replace token'" color="error" @click="confirm" />
        <UButton color="neutral" variant="ghost" label="Cancel" @click="confirming = null" />
      </template>
    </UModal>
  </SettingsSection>
</template>
