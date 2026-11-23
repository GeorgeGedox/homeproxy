<script setup lang="ts">
import type { components } from "~~/api/schema";

const network = defineModel<components["schemas"]["Network"]>({ required: true });
</script>

<template>
  <SettingsSection id="network" title="Network" description="Which clients count as LAN, and the ports HAProxy listens on.">
    <UFormField label="LAN ranges" name="network.lan_cidrs" hint="Press Enter after each range" help="Clients in these ranges count as LAN: only they reach LAN-only sites and this dashboard. For example 192.168.1.0/24.">
      <UInputTags add-on-blur v-model="network.lan_cidrs" class="w-full" />
    </UFormField>
    <UFormField label="Proxy LAN IP" name="network.haproxy_lan_ip" help="The LAN address of the machine running HomeProxy, for example 192.168.1.10. The DNS server answers your sites' names with it."><UInput v-model="network.haproxy_lan_ip" class="w-full" /></UFormField>
    <UFormField label="Public IP" name="network.public_ip" hint="auto, or an IPv4 address" help="Your home's address on the internet, kept in the Cloudflare DDNS record. auto looks it up.">
      <UInput v-model="network.public_ip" class="w-full" />
    </UFormField>
    <div class="grid gap-4 sm:grid-cols-2">
      <UFormField label="HTTP port" name="network.http_port" help="Usually 80. Change it only if another program uses it."><UInputNumber v-model="network.http_port" :increment="false" :decrement="false" :min="1" :max="65535" :format-options="{ useGrouping: false }" class="w-full" /></UFormField>
      <UFormField label="HTTPS port" name="network.https_port" help="Usually 443. Change it only if another program uses it."><UInputNumber v-model="network.https_port" :increment="false" :decrement="false" :min="1" :max="65535" :format-options="{ useGrouping: false }" class="w-full" /></UFormField>
    </div>
  </SettingsSection>
</template>
