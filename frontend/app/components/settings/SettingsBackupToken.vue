<script setup lang="ts">
const { status, token, error, busy, load, create, remove, copy } = useBackup();
onMounted(() => void load());

const confirming = ref<"replace" | "remove" | null>(null);
const confirmOpen = computed({ get: () => confirming.value !== null, set: (open: boolean) => void (open || (confirming.value = null)) });
/** The download with the token. --insecure drops the CA check, which a self-signed certificate fails; the pin still refuses any other server. */
const command = `curl --fail --insecure --pinnedpubkey "sha256//<key pin>" \\
  --header "Authorization: Bearer <token>" \\
  --remote-name --remote-header-name \\
  https://<dashboard address>/api/v1/backup`;
const here = window.location.host;
// A new token is the moment the admin needs the command, so it opens.
const helpOpen = ref(false);
watch(token, (value) => {
  if (value) helpOpen.value = true;
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
          <UButton icon="lucide:copy" label="Copy token" color="neutral" variant="outline" class="self-start sm:self-auto" @click="copy(token, 'backup token')" />
        </div>
      </template>
    </UAlert>
    <UCollapsible v-model:open="helpOpen" :unmount-on-hide="false" class="border-t border-default pt-4">
      <UButton
        label="Use the token" color="neutral" variant="ghost" trailing-icon="lucide:chevron-down" class="group -mx-2.5 font-medium"
        :ui="{ trailingIcon: 'transition-transform group-data-[state=open]:rotate-180' }"
      />
      <template #content>
        <div class="space-y-3 pt-2 text-sm">
          <p class="text-muted">A script on another machine, for example a NAS, downloads a backup with this command. Replace the three values in angle brackets.</p>
          <pre class="overflow-x-auto rounded-md border border-default bg-muted px-3 py-2 font-mono text-xs text-highlighted">{{ command }}</pre>
          <dl class="divide-y divide-default rounded-md border border-default">
            <div class="flex flex-col gap-0.5 px-3 py-2 sm:flex-row sm:gap-4">
              <dt class="shrink-0 pt-0.5 font-mono text-xs text-highlighted sm:w-40">&lt;dashboard address&gt;</dt>
              <dd class="min-w-0 text-muted">The address and port you open the dashboard on. For this dashboard: <code class="font-mono text-xs text-highlighted">{{ here }}</code></dd>
            </div>
            <div class="flex flex-col gap-0.5 px-3 py-2 sm:flex-row sm:gap-4">
              <dt class="shrink-0 pt-0.5 font-mono text-xs text-highlighted sm:w-40">&lt;token&gt;</dt>
              <dd class="min-w-0 text-muted">The backup token. It shows only once, when you create or replace it.</dd>
            </div>
            <div class="flex flex-col gap-0.5 px-3 py-2 sm:flex-row sm:gap-4">
              <dt class="shrink-0 pt-0.5 font-mono text-xs text-highlighted sm:w-40">&lt;key pin&gt;</dt>
              <dd v-if="status?.key_pin" class="min-w-0 space-y-1 text-muted">
                <div class="flex items-center gap-2">
                  <code class="min-w-0 font-mono text-xs break-all text-highlighted" data-testid="backup-key-pin">{{ status.key_pin }}</code>
                  <UButton icon="lucide:copy" aria-label="Copy key pin" color="neutral" variant="ghost" size="xs" @click="copy(status.key_pin, 'key pin')" />
                </div>
                <p>The pin of this dashboard's certificate. With it, curl refuses any other server, so <code class="font-mono text-xs whitespace-nowrap">--insecure</code> is safe here.</p>
              </dd>
              <dd v-else class="min-w-0 text-muted">The dashboard certificate could not be read, so there is no pin to show.</dd>
            </div>
          </dl>
          <p class="text-muted">If your computer trusts the dashboard certificate, leave out <code class="font-mono text-xs whitespace-nowrap">--insecure</code> and <code class="font-mono text-xs whitespace-nowrap">--pinnedpubkey</code>.</p>
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
