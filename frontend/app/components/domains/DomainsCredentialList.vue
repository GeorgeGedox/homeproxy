<script setup lang="ts">
import type { DnsProvider } from "~/composables/useDnsProviders";

const props = defineProps<{ credentials: StoredCredential[]; loadError: string; providers: DnsProvider[] }>();
const emit = defineEmits<{ changed: [] }>();
const api = useApi();

const replacing = ref<StoredCredential>();
const replaceOpen = computed({ get: () => Boolean(replacing.value), set: () => (replacing.value = undefined) });
const deleting = ref<StoredCredential>();
const deleteOpen = computed({ get: () => Boolean(deleting.value), set: () => (deleting.value = undefined) });
const deleteError = ref("");

const provider = (code: string) => props.providers.find((p) => p.code === code);
/** Each variable as the admin reads it: a secret only as "set". */
const values = (credential: StoredCredential) =>
  Object.entries(credential.variables).map(([name, value]) => ({ name: name === "api_token" ? "API token" : name, env: name !== "api_token", value: value ?? "set" }));

function askDelete(credential: StoredCredential): void {
  deleteError.value = "";
  deleting.value = credential;
}

async function remove(): Promise<void> {
  const name = deleting.value?.name;
  if (!name) return;
  const { response, error } = await api.DELETE("/api/v1/credentials/{name}", { params: { path: { name } } });
  if (!response.ok) return void (deleteError.value = apiErrorMessage(error, "The credentials were not deleted."));
  deleting.value = undefined;
  emit("changed");
}
</script>

<template>
  <section class="rounded-lg border border-default" aria-label="DNS credentials">
    <header class="rounded-t-lg bg-muted px-4 py-3" :class="{ 'border-b border-default': credentials.length || loadError }">
      <h2 class="text-sm text-highlighted">DNS credentials</h2>
      <p class="text-xs text-muted">Saved at once, not staged. A domain picks them under HTTPS.</p>
    </header>
    <p v-if="loadError" class="px-4 py-2 text-sm text-error">{{ loadError }}</p>
    <ul v-else-if="credentials.length" class="divide-y divide-default">
      <li v-for="credential in credentials" :key="credential.name" class="flex flex-wrap items-center gap-x-3 gap-y-1 px-4 py-2 text-sm">
        <div class="min-w-0 flex-1">
          <p class="flex items-baseline gap-2">
            <span class="truncate font-mono text-highlighted">{{ credential.name }}</span>
            <span class="text-xs text-muted">{{ provider(credential.provider)?.name ?? credential.provider }}</span>
          </p>
          <p class="text-xs text-muted">
            <span v-for="variable in values(credential)" :key="variable.name">
              <span :class="{ 'font-mono': variable.env }">{{ variable.name }}</span> <span class="text-default">{{ variable.value }}</span><span class="text-dimmed"> · </span>
            </span>{{ credential.zones.length ? `Used by ${credential.zones.join(", ")}` : "Not used" }}
          </p>
        </div>
        <UButton
          label="Replace" color="neutral" variant="ghost" size="xs" :disabled="!provider(credential.provider)"
          :aria-label="`Replace ${credential.name}`" @click="replacing = credential"
        />
        <UButton
          v-if="!credential.zones.length" label="Delete" color="error" variant="ghost" size="xs"
          :aria-label="`Delete ${credential.name}`" @click="askDelete(credential)"
        />
      </li>
    </ul>
    <p v-else class="px-4 py-2 text-sm text-muted">None yet. They are added with a domain that gets Let's Encrypt certificates.</p>

    <DomainsCredentialReplace
      v-if="replacing && provider(replacing.provider)" v-model:open="replaceOpen" :credential="replacing"
      :provider="provider(replacing.provider)!" @saved="emit('changed')"
    />
    <UModal v-model:open="deleteOpen" :title="`Delete ${deleting?.name}?`" description="No domain uses them. HomeProxy forgets the stored values at once; this cannot be undone.">
      <template v-if="deleteError" #body>
        <p class="text-sm text-error">{{ deleteError }}</p>
      </template>
      <template #footer>
        <UButton color="error" variant="soft" label="Delete credentials" @click="remove" />
        <UButton color="neutral" variant="ghost" label="Keep them" @click="deleting = undefined" />
      </template>
    </UModal>
  </section>
</template>
