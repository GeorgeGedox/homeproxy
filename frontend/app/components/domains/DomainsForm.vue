<script setup lang="ts">
import type { Domain } from "~/composables/useDomains";

const props = defineProps<{ domain?: Domain }>();
/** Whether the form holds edits not yet staged, for the page's leave guard. */
const dirty = defineModel<boolean>("dirty", { required: true });
const emit = defineEmits<{ saved: [] }>();

const api = useApi();
const pending = usePending();
const { providers, load: loadProviders } = useDnsProviders();
const { form, providerCode, reset, pickProvider, pickStored } = useDomainForm();
const stored = ref<StoredCredential[]>([]);
/** Without the stored credentials, a save could drop typed keys or name new ones over stored ones. */
const loadError = ref("");
const busy = ref(false);
const formRef = useTemplateRef("formRef");
const { error: refused, show: showRefused, clear: clearRefused } = useFormError(formRef);
const clean = ref("");
watchEffect(() => (dirty.value = Boolean(clean.value) && JSON.stringify(form) !== clean.value));

const kinds: { label: string; description: string; value: HttpsKind }[] = [
  { label: "No certificate", description: "Plain HTTP only, for a network you trust", value: "none" },
  { label: "Self-signed", description: "HTTPS that browsers warn about, no account needed", value: "self-signed" },
  { label: "Let's Encrypt via Cloudflare", description: "Trusted HTTPS, plus Cloudflare DNS records and DDNS", value: "cloudflare" },
  { label: "Let's Encrypt via another DNS provider", description: "Trusted HTTPS through your DNS provider's API", value: "other" },
];
const otherProviders = computed(() => providers.value.filter((p) => p.code !== CLOUDFLARE).map((p) => ({ label: p.name, value: p.code })));
const provider = computed(() => providers.value.find((p) => p.code === providerCode.value));
const storedHere = computed(() => stored.value.filter((credential) => credential.provider === providerCode.value));
const zone = computed(() => props.domain?.zone ?? form.zone.trim());

async function start(): Promise<void> {
  reset(props.domain);
  clearRefused();
  const [, list] = await Promise.all([loadProviders(), api.GET("/api/v1/credentials")]);
  stored.value = list.data ?? [];
  loadError.value = list.data ? "" : apiErrorMessage(list.error, "The stored credentials could not be loaded.");
  clean.value = JSON.stringify(form);
}
onMounted(start);

/** A new choice of provider starts from its stored credentials; the domain's own provider keeps its own. */
function pick(code: string): void {
  pickProvider(code, stored.value);
  if (props.domain && code === props.domain.dns_provider) form.stored = props.domain.credentials ?? "";
}

function choose(kind: HttpsKind): void {
  form.https = kind;
  if (kind === "cloudflare") pick(CLOUDFLARE);
  if (kind === "other") pick("");
}

async function saveCredentials(): Promise<boolean> {
  const update = credentialUpdate(form, stored.value.find((credential) => credential.name === form.stored));
  if (!update) return true;
  const { response, error: failure } = await api.PUT("/api/v1/credentials/{name}", { params: { path: { name: form.stored } }, body: update });
  if (!response.ok) showRefused(failure, "The credentials were not saved.");
  return response.ok;
}

async function submit(): Promise<void> {
  if (loadError.value) return;
  busy.value = true;
  clearRefused();
  try {
    if (!(await saveCredentials())) return;
    const { response, error: failure } = props.domain
      ? await api.PUT("/api/v1/domains/{zone}", { params: { path: { zone: props.domain.zone } }, body: domainBody(form) })
      : await api.POST("/api/v1/domains", { body: domainBody(form) });
    if (!response.ok) {
      showRefused(failure, "The domain was not staged.");
      return;
    }
    clean.value = JSON.stringify(form);
    await Promise.all([pending.refresh(), nextTick()]);
    emit("saved");
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <UForm ref="formRef" :state="form" :validate-on="[]" class="max-w-2xl space-y-6" @submit="submit">
    <UFormField v-if="!domain" label="Domain" name="zone" description="Your sites use it and any name under it, such as app.example.com" required>
      <UInput v-model="form.zone" placeholder="example.com" class="w-full" />
    </UFormField>

    <section class="space-y-4">
      <h3 class="text-base font-medium text-highlighted">HTTPS</h3>
      <URadioGroup
        :model-value="form.https" :items="kinds" variant="card" color="neutral" legend="HTTPS"
        :ui="{ fieldset: 'grid grid-cols-1 gap-2', legend: 'sr-only' }" @update:model-value="choose($event as HttpsKind)"
      />
      <UFormField v-if="form.https === 'other'" label="DNS provider" name="dns_provider" required>
        <USelectMenu
          :model-value="form.provider || undefined" :items="otherProviders" value-key="value" placeholder="Find your provider" class="w-full"
          @update:model-value="pick($event ?? '')"
        />
      </UFormField>
      <USwitch
        v-if="form.https !== 'none'" v-model="form.wildcard" :label="`One wildcard certificate for *.${zone || 'example.com'}`"
        description="Otherwise each host gets a certificate of its own"
      />
    </section>

    <section v-if="provider" class="space-y-4 border-t border-default pt-6">
      <h3 class="text-base font-medium text-highlighted">{{ provider.name }} credentials</h3>
      <DomainsCredentials
        :chosen="form.stored" v-model:name="form.credentials" @choose="pickStored($event, stored)" v-model:token="form.token" v-model:env="form.env"
        v-model:replacing="form.replacing" :provider="provider" :stored="storedHere" :zone="zone"
      />
    </section>

    <section v-if="form.https === 'cloudflare'" class="space-y-4 border-t border-default pt-6">
      <h3 class="text-base font-medium text-highlighted">Cloudflare</h3>
      <UFormField label="DDNS record" name="cloudflare.ddns_record" description="A name kept on your public IP, for records that point at home. HomeProxy creates it if it does not exist. Empty for none.">
        <UFieldGroup class="w-full">
          <UInput v-model="form.ddnsName" placeholder="home" class="min-w-0 flex-1" />
          <UBadge :label="`.${zone || 'example.com'}`" color="neutral" variant="outline" size="lg" class="font-mono text-muted" />
        </UFieldGroup>
      </UFormField>
      <USwitch v-model="form.ddnsUpdate" label="Keep the DDNS record on the current public IP" />
      <USwitch v-model="form.proxied" label="Proxy new records through Cloudflare" description="A route can still choose for itself" />
    </section>

    <div class="space-y-3 border-t border-default pt-6">
      <UAlert v-if="loadError || refused" color="error" variant="subtle" :description="loadError || refused" />
      <div class="flex gap-2">
        <UButton type="submit" :loading="busy" :disabled="Boolean(loadError)" label="Stage change" />
        <UButton to="/domains" color="neutral" variant="ghost" label="Cancel" />
      </div>
    </div>
  </UForm>
</template>
