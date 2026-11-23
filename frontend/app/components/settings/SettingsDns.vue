<script setup lang="ts">
import type { components } from "~~/api/schema";

const dns = defineModel<components["schemas"]["DnsServer"]>({ required: true });
</script>

<template>
  <SettingsSection id="dns" title="DNS server" description="The internal DNS server that answers for your routes.">
    <UFormField label="DNS server address" name="dns_server.listen" hint="ip:port" help="Where the DNS server listens, for example 192.168.1.10:5335. Point AdGuard Home or your router here for your domains."><UInput v-model="dns.listen" class="w-full" /></UFormField>
    <UFormField label="DNS upstreams" name="dns_server.forward_unknown_to" hint="Press Enter after each address" help="Where other names in your domains go, such as their public records, for example 1.1.1.1. Not AdGuard Home: the query would loop.">
      <UInputTags add-on-blur v-model="dns.forward_unknown_to" class="w-full" />
    </UFormField>
  </SettingsSection>
</template>
