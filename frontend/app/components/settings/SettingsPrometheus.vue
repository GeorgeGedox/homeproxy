<script setup lang="ts">
import type { components } from "~~/api/schema";

const prometheus = defineModel<components["schemas"]["Prometheus"]>({ required: true });
</script>

<template>
  <SettingsSection id="prometheus" title="Prometheus" description="HAProxy's metrics exporter at /metrics, for LAN clients only.">
    <USwitch v-model="prometheus.enabled" label="Prometheus exporter" />
    <UFormField v-if="prometheus.enabled" label="Prometheus listen address" name="prometheus.listen" hint="ip:port" help="Where Prometheus scrapes /metrics, for example 192.168.1.10:9101.">
      <UInput :model-value="prometheus.listen ?? ''" class="w-full" @update:model-value="prometheus.listen = $event || null" />
    </UFormField>
  </SettingsSection>
</template>
