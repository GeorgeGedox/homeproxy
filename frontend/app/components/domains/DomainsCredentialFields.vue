<script setup lang="ts">
import type { DnsProvider } from "~/composables/useDnsProviders";

const props = defineProps<{
  provider: DnsProvider;
  /** The stored credentials being replaced, or none for new ones. */
  current?: StoredCredential;
  zone: string;
}>();
const token = defineModel<string>("token", { required: true });
const env = defineModel<Record<string, string>>("env", { required: true });

const isCloudflare = computed(() => props.provider.code === CLOUDFLARE);
const pathHint = (variable: string): string | undefined => (/_(FILE|PATH)$/.test(variable) ? "A file inside /data/secrets" : undefined);
const placeholder = (variable: string) => (props.current?.variables[variable] === null ? "Stored, type to replace" : undefined);
// The same words the server uses to keep a value secret; a region or a URL stays readable while typed.
const SECRET = /KEY|SECRET|TOKEN|PASS|AUTH|PRIVATE|CREDENTIAL|SIGNATURE|OTP|SERVICE_ACCOUNT/;
const inputType = (variable: string) => (SECRET.test(variable) ? "password" : "text");
</script>

<template>
  <UFormField
    v-if="isCloudflare" label="API token" name="api_token" :required="!current"
    :description="`Needs Zone Read and DNS Edit on ${zone || 'the zone'}`"
  >
    <UInput v-model="token" type="password" autocomplete="off" :placeholder="current ? 'Stored, type to replace' : undefined" class="w-full" />
  </UFormField>
  <template v-else>
    <UFormField
      v-for="variable in provider.credentials" :key="variable.name" :label="variable.name" :name="`env.${variable.name}`"
      :description="variable.description" :hint="pathHint(variable.name)" :required="!placeholder(variable.name)"
    >
      <UInput v-model="env[variable.name]" :type="inputType(variable.name)" autocomplete="off" :placeholder="placeholder(variable.name)" class="w-full" />
    </UFormField>
    <UCollapsible v-if="provider.options.length" :unmount-on-hide="false">
      <UButton label="More options" color="neutral" variant="ghost" trailing-icon="lucide:chevron-down" class="group -mx-2.5" :ui="{ trailingIcon: 'transition-transform group-data-[state=open]:rotate-180' }" />
      <template #content>
        <div class="space-y-4 pt-2">
          <UFormField
            v-for="variable in provider.options" :key="variable.name" :label="variable.name" :name="`env.${variable.name}`"
            :description="variable.description" :hint="pathHint(variable.name)"
          >
            <UInput v-model="env[variable.name]" :type="inputType(variable.name)" autocomplete="off" :placeholder="placeholder(variable.name)" class="w-full" />
          </UFormField>
        </div>
      </template>
    </UCollapsible>
  </template>
</template>
