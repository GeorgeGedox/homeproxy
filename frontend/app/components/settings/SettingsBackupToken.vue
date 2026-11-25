<script setup lang="ts">
const { status, token, error, busy, load, create, remove, copy } = useBackup();
onMounted(() => void load());

const confirming = ref<"replace" | "remove" | null>(null);
const confirmOpen = computed({ get: () => confirming.value !== null, set: (open: boolean) => void (open || (confirming.value = null)) });
const commands = computed(() => backupCommands(window.location.host));
// A new token is the moment the admin needs the script, so it opens.
const scriptOpen = ref(false);
watch(token, (value) => {
  if (value) scriptOpen.value = true;
});

async function confirm(): Promise<void> {
  const action = confirming.value;
  confirming.value = null;
  await (action === "remove" ? remove() : create());
}
</script>

<template>
  <SettingsSection title="Backup token" description="Lets a script download backups, for example every night to a NAS. It cannot log in or do anything else.">
    <UAlert v-if="error" color="error" variant="subtle" :description="error" />
    <div v-if="status" class="flex flex-wrap items-center justify-between gap-3">
      <p class="inline-flex items-center gap-2 text-sm" data-testid="backup-token-state">
        <StatusLed :tone="status.set ? 'up' : 'off'" />
        <span :class="status.set ? 'text-highlighted' : 'text-muted'">{{ status.set ? "Set" : "Not set" }}</span>
        <span v-if="status.set && status.created_at" class="text-muted">· created {{ dateTime(status.created_at) }}</span>
      </p>
      <div class="flex gap-2">
        <UButton
          :label="status.set ? 'Replace token' : 'Create token'" color="neutral" variant="outline" :loading="busy"
          @click="status.set ? (confirming = 'replace') : create()"
        />
        <UButton v-if="status.set" label="Remove token" color="error" variant="ghost" @click="confirming = 'remove'" />
      </div>
    </div>
    <UAlert v-if="token" color="warning" variant="subtle" title="Copy the token now" description="It is shown only once. HomeProxy keeps only a hash of it.">
      <template #actions>
        <div class="flex w-full flex-col gap-2 sm:flex-row sm:items-center">
          <code class="min-w-0 flex-1 rounded-md border border-default bg-default px-3 py-2 font-mono text-xs break-all text-highlighted" data-testid="backup-token">{{ token }}</code>
          <UButton icon="lucide:copy" label="Copy token" color="neutral" variant="outline" class="self-start sm:self-auto" @click="copy" />
        </div>
      </template>
    </UAlert>
    <UCollapsible v-model:open="scriptOpen" :unmount-on-hide="false" class="border-t border-default pt-4">
      <UButton
        label="Nightly script" color="neutral" variant="ghost" trailing-icon="lucide:chevron-down" class="group -mx-2.5 font-medium"
        :ui="{ trailingIcon: 'transition-transform group-data-[state=open]:rotate-180' }"
      />
      <template #content>
        <div class="space-y-2 pt-2">
          <p class="text-sm text-muted">Put the token in <code class="font-mono text-highlighted">HOMEPROXY_BACKUP_TOKEN</code>. The script checks the dashboard certificate on every download.</p>
          <pre class="overflow-x-auto rounded-md border border-default bg-muted px-3 py-2 font-mono text-xs text-highlighted">{{ commands }}</pre>
        </div>
      </template>
    </UCollapsible>
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
