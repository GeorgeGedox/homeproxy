<script setup lang="ts">
const { form, needed, needs, detected, mismatch, error, busy, submit } = useSetupWizard();
// The dashboard stays on the address this page was opened on.
const here = window.location.host;
/** What setup found and writes, as docs/install.md step 5 lists it. Undetected values are asked for instead. */
const found = computed(() => [
  { label: "LAN ranges", value: detected.value?.network?.lan_cidrs?.join(", ") },
  { label: "Proxy LAN IP", value: detected.value?.network?.haproxy_lan_ip },
  { label: "DNS server address", value: detected.value?.dns_server?.listen },
  { label: "Dashboard address", value: here },
].filter((item) => item.value));
/** The two ways to finish setup. Both use the setup token above them. */
const ways = [{ label: "New setup", slot: "new" as const }, { label: "Restore from backup", slot: "restore" as const }];
const restored = ref(false);
</script>

<template>
  <section v-if="restored" class="space-y-4" aria-label="Backup restored">
    <h1 class="flex items-center gap-2 text-lg text-highlighted"><StatusLed tone="up" />Backup restored</h1>
    <BackupRestored />
  </section>
  <div v-else class="space-y-4">
    <div class="space-y-1">
      <h1 class="text-lg text-highlighted">Set up HomeProxy</h1>
      <p class="text-sm text-muted">The setup token is in the container log:</p>
      <code class="block rounded-md border border-default bg-muted px-3 py-2 font-mono text-xs break-all text-highlighted">docker compose logs homeproxy | grep -A12 "not set up yet"</code>
    </div>
    <UFormField label="Setup token" name="token">
      <UInput v-model="form.token" form="setup-new" type="password" autocomplete="off" required class="w-full" />
    </UFormField>
    <UTabs :items="ways" variant="link" color="neutral" class="w-full">
      <template #new>
        <form id="setup-new" class="space-y-4 pt-2" @submit.prevent="submit">
          <UFormField label="Admin password" name="password" hint="At least 10 characters">
            <UInput v-model="form.password" type="password" minlength="10" autocomplete="new-password" required class="w-full" />
          </UFormField>
          <UFormField
            label="Repeat the password"
            name="confirm"
            :error="mismatch ? 'The passwords do not match.' : undefined"
          >
            <UInput v-model="form.confirm" type="password" autocomplete="new-password" required class="w-full" />
          </UFormField>
          <section v-if="detected" class="rounded-lg border border-default" aria-label="Found on this machine" data-testid="setup-summary">
            <h2 class="rounded-t-lg border-b border-default bg-muted px-4 py-2 text-sm text-highlighted">HomeProxy found these</h2>
            <dl class="divide-y divide-default text-sm">
              <div v-for="item in found" :key="item.label" class="flex justify-between gap-3 px-4 py-1.5">
                <dt class="text-muted">{{ item.label }}</dt>
                <dd class="min-w-0 truncate text-right font-mono text-highlighted">{{ item.value }}</dd>
              </div>
            </dl>
            <p class="border-t border-default px-4 py-2 text-xs text-muted">Change them later in Settings.</p>
          </section>
          <UFormField
            v-for="path in needs"
            :key="path"
            :label="needLabels[path]?.label ?? path"
            :name="path"
            :help="needLabels[path]?.help"
          >
            <UInput v-model="needed[path]" :type="path.endsWith('_port') ? 'number' : 'text'" required class="w-full" />
          </UFormField>
          <UAlert v-if="error" color="error" variant="subtle" :description="error" />
          <UButton type="submit" block :loading="busy" :label="busy ? 'Applying the first config…' : 'Finish setup'" />
          <p v-if="busy" class="text-center text-xs text-muted">This takes a few seconds. Then you are signed in.</p>
        </form>
      </template>
      <template #restore>
        <SetupRestore class="pt-2" :token="form.token" @restored="restored = true" />
      </template>
    </UTabs>
  </div>
</template>
