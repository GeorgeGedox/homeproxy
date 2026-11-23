<script setup lang="ts">
import type { DnsProvider } from "~/composables/useDnsProviders";

const props = defineProps<{
  provider: DnsProvider;
  /** Stored credentials of this provider. */
  stored: StoredCredential[];
  zone: string;
  /** The stored credentials in use, or "" for new ones. Changed through `choose`. */
  chosen: string;
}>();
const emit = defineEmits<{ choose: [string] }>();
const name = defineModel<string>("name", { required: true });
const token = defineModel<string>("token", { required: true });
const env = defineModel<Record<string, string>>("env", { required: true });
const replacing = defineModel<boolean>("replacing", { required: true });

const NEW = "new";
const choices = computed(() => [
  ...props.stored.map((credential) => ({
    label: `Stored: ${credential.name}`,
    description: credential.zones.length ? `Used by ${credential.zones.join(", ")}` : "Not used by a domain",
    value: credential.name,
  })),
  { label: "New credentials", description: props.provider.code === CLOUDFLARE ? "Enter a new API token" : `Enter new ${props.provider.name} keys`, value: NEW },
]);
const choice = computed({ get: () => props.chosen || NEW, set: (value: string) => emit("choose", value === NEW ? "" : value) });
const current = computed(() => props.stored.find((credential) => credential.name === props.chosen));
/** Show the input fields: for new credentials, or while replacing stored ones. */
const editing = computed(() => !current.value || replacing.value);
const others = computed(() => current.value?.zones.filter((zone) => zone !== props.zone) ?? []);

// Replacing starts from the stored values that are not secret, so the admin sees what stays.
watch(replacing, (on) => {
  if (on && current.value) env.value = Object.fromEntries(Object.entries(current.value.variables).filter(([, value]) => value !== null)) as Record<string, string>;
});
</script>

<template>
  <div class="space-y-4">
    <URadioGroup v-if="stored.length" v-model="choice" :items="choices" variant="card" color="neutral" legend="Credentials" :ui="{ fieldset: 'grid grid-cols-1 gap-2 sm:grid-cols-2', legend: 'sr-only' }" />

    <div v-if="current && !replacing" class="space-y-2 rounded-md border border-default p-3 text-sm">
      <dl class="space-y-1">
        <div v-for="(value, variable) in current.variables" :key="variable" class="flex items-baseline gap-3">
          <dt v-if="variable === 'api_token'" class="text-muted">API token</dt>
          <dt v-else class="min-w-0 truncate font-mono text-xs text-muted">{{ variable }}</dt>
          <dd class="text-highlighted">{{ value ?? "Set" }}</dd>
        </div>
      </dl>
      <UButton label="Replace the stored values" color="neutral" variant="outline" size="xs" @click="replacing = true" />
    </div>

    <template v-if="editing">
      <p v-if="current && others.length" class="text-sm text-primary">
        Saved at once, for every domain that uses {{ current.name }}: {{ others.join(", ") }} too
      </p>
      <UFormField v-if="!current" label="Name" name="credentials" description="Other domains on the same provider can use them later" required>
        <UInput v-model="name" class="w-full" />
      </UFormField>
      <DomainsCredentialFields v-model:token="token" v-model:env="env" :provider="provider" :current="current" :zone="zone" />
      <UButton v-if="current" label="Keep the stored values" color="neutral" variant="ghost" size="xs" @click="replacing = false" />
    </template>
  </div>
</template>
