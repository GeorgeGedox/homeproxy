<script setup lang="ts">
import type { components } from "~~/api/schema";

const certificates = defineModel<components["schemas"]["CertificateSettings"]>({ required: true });
</script>

<template>
  <SettingsSection id="certificates" title="Certificates" description="Which Let's Encrypt service issues the certificates of domains with a DNS provider.">
    <USwitch
      v-model="certificates.staging" label="Use Let's Encrypt staging"
      description="For testing a DNS provider without its rate limits. Browsers do not trust staging certificates, so turn it off before you rely on your sites."
    />
  </SettingsSection>
</template>
