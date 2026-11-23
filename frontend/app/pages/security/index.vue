<script setup lang="ts">
const crowdsec = useDecisions();
const allowlist = useTemplateRef("allowlist");
const banOpen = ref(false);
</script>

<template>
  <UDashboardPanel id="security">
    <template #header>
      <UDashboardNavbar title="Security">
        <template #right>
          <UButton icon="lucide:shield-alert" label="Ban an address" :disabled="crowdsec.unconfigured.value" @click="banOpen = true" />
        </template>
      </UDashboardNavbar>
    </template>
    <template #body>
      <SecurityDecisions :crowdsec="crowdsec" @allowlisted="allowlist?.load()" />
      <SecurityAllowlist ref="allowlist" />
      <SecurityCountries />
      <SecurityBanDialog v-model:open="banOpen" :crowdsec="crowdsec" />
    </template>
  </UDashboardPanel>
</template>
