<script setup lang="ts">
import type { components } from "~~/api/schema";

const cloudflare = defineModel<components["schemas"]["CloudflareSettings"]>({ required: true });
</script>

<template>
  <SettingsSection id="cloudflare" title="Cloudflare" description="DNS records for your public sites and dynamic DNS. Each domain's token is set on Domains.">
    <USwitch v-model="cloudflare.enabled" label="Keep Cloudflare DNS records" description="Off: HomeProxy makes no Cloudflare DNS calls and leaves your records as they are." />
    <UFormField
      label="Sync interval" name="cloudflare.reconcile_interval"
      description="How often records are synced with your sites besides after each apply. At least 5m; use s, m, h or d."
    >
      <UInput v-model="cloudflare.reconcile_interval" placeholder="15m" class="w-32" />
    </UFormField>
  </SettingsSection>
</template>
