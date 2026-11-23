<script setup lang="ts">
import type { DnsProvider } from "~/composables/useDnsProviders";

const props = defineProps<{ credential: StoredCredential; provider: DnsProvider }>();
const open = defineModel<boolean>("open", { required: true });
const emit = defineEmits<{ saved: [] }>();

const api = useApi();
const token = ref("");
// Starts from the stored values that are not secret, so the admin sees what stays.
const env = ref(Object.fromEntries(Object.entries(props.credential.variables).filter(([, value]) => value !== null)) as Record<string, string>);
const busy = ref(false);
const formRef = useTemplateRef("formRef");
const { error, show, clear } = useFormError(formRef);
const zones = computed(() => props.credential.zones.join(", "));

async function submit(): Promise<void> {
  const update = credentialUpdate({ stored: props.credential.name, replacing: true, token: token.value, env: env.value }, props.credential);
  if (!update) return void (error.value = "Type the new token, or Cancel to keep the stored one.");
  busy.value = true;
  clear();
  const { response, error: failure } = await api.PUT("/api/v1/credentials/{name}", { params: { path: { name: props.credential.name } }, body: update });
  busy.value = false;
  if (!response.ok) return show(failure, "The credentials were not saved.");
  open.value = false;
  emit("saved");
}
</script>

<template>
  <UModal v-model:open="open" :title="`Replace ${credential.name}`" :description="zones ? `Saved at once, for ${zones}` : 'Saved at once'">
    <template #body>
      <UForm id="credential-form" ref="formRef" :state="{ token, env }" :validate-on="[]" class="space-y-4" @submit="submit">
        <DomainsCredentialFields v-model:token="token" v-model:env="env" :provider="provider" :current="credential" :zone="credential.zones[0] ?? ''" />
        <UAlert v-if="error" color="error" variant="subtle" :description="error" />
      </UForm>
    </template>
    <template #footer>
      <UButton type="submit" form="credential-form" :loading="busy" label="Save" />
      <UButton color="neutral" variant="ghost" label="Cancel" @click="open = false" />
    </template>
  </UModal>
</template>
