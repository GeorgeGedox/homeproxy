<script setup lang="ts">
import type { DropdownMenuItem } from "@nuxt/ui";
import type { DomainEntry } from "~/composables/useDomains";

const props = defineProps<{
  entry: DomainEntry;
  cloudflareOn: boolean;
  /** The DNS provider's display name for its code. */
  providerName: (code: string) => string;
}>();
const emit = defineEmits<{ edit: []; remove: []; renew: [string] }>();
const domain = computed(() => props.entry.domain);

/** How the domain gets HTTPS, its DDNS record and why it has no Cloudflare records, in one line. */
const facts = computed(() => {
  const provider = domain.value.dns_provider;
  const https = !provider ? "No certificate" : provider === SELF_SIGNED ? "Self-signed" : `Let's Encrypt via ${props.providerName(provider)}`;
  const ddns = domain.value.cloudflare?.ddns_record;
  // Why the Cloudflare features are off matters only while the integration is on.
  const cloudflareOff = !props.cloudflareOn || !domain.value.cloudflare_off_reason ? []
    : [provider && provider !== SELF_SIGNED ? `No Cloudflare records: DNS is on ${props.providerName(provider)}` : "No Cloudflare records: no DNS provider"];
  return [domain.value.wildcard_cert ? `${https}, wildcard` : https, ...(ddns ? [`DDNS ${ddns}`] : []), ...cloudflareOff];
});
const sites = computed(() => (props.entry.sites === 1 ? "1 site" : `${props.entry.sites || "No"} sites`));

const menu = computed((): DropdownMenuItem[] => [
  { label: "Edit", icon: "lucide:pencil", onSelect: () => emit("edit") },
  { type: "separator" },
  { label: "Remove", icon: "lucide:trash-2", color: "error", onSelect: () => emit("remove") },
]);
</script>

<template>
  <section class="rounded-lg border border-default" :aria-label="domain.zone">
    <header class="flex items-center gap-3 rounded-t-lg bg-muted px-4 py-3" :class="entry.certificates.length || domain.dns_provider ? 'border-b border-default' : 'rounded-b-lg'">
      <UTooltip :text="entry.text">
        <StatusLed :tone="entry.tone" :label="entry.text" />
      </UTooltip>
      <div class="min-w-0 flex-1">
        <h2 class="flex items-baseline gap-2 font-mono text-sm text-highlighted">
          <span class="truncate">{{ domain.zone }}</span><span v-if="entry.staged" class="font-sans text-xs text-primary">staged</span>
        </h2>
        <!-- One fact per line on a phone; on one line, separated, where there is room. -->
        <p class="flex flex-col text-xs text-muted sm:flex-row sm:flex-wrap sm:gap-x-1.5">
          <span v-for="(fact, index) in facts" :key="fact"><span v-if="index" class="mr-1.5 hidden text-dimmed sm:inline" aria-hidden="true">·</span>{{ fact }}</span>
          <span><span class="mr-1.5 hidden text-dimmed sm:inline" aria-hidden="true">·</span><ULink to="/routes" class="text-muted hover:text-highlighted">{{ sites }}</ULink></span>
        </p>
      </div>
      <UDropdownMenu :items="menu" :content="{ align: 'end' }">
        <UButton icon="lucide:ellipsis" color="neutral" variant="ghost" size="sm" :aria-label="`Actions for ${domain.zone}`" />
      </UDropdownMenu>
    </header>

    <ul v-if="entry.certificates.length" class="divide-y divide-default" :aria-label="`Certificates of ${domain.zone}`">
      <li v-for="cert in entry.certificates" :key="cert.name" class="flex flex-wrap items-center gap-x-3 gap-y-1 px-4 py-2 text-sm">
        <StatusLed :tone="cert.tone" :label="cert.text" />
        <span class="min-w-0 flex-1 truncate font-mono text-highlighted">{{ cert.names.join(", ") }}</span>
        <UTooltip :text="cert.expires ? `Expires ${cert.expires}` : undefined" :disabled="!cert.expires">
          <span
            class="order-last line-clamp-2 basis-full pl-5 text-xs sm:order-none sm:basis-auto sm:pl-0" :title="cert.text"
            :class="cert.tone === 'down' ? 'text-error' : 'text-muted'"
          >
            <template v-if="cert.issuer">{{ cert.issuer }} · </template>{{ cert.text }}
          </span>
        </UTooltip>
        <UButton
          v-if="!cert.issuing" :label="cert.installed ? 'Renew' : cert.tone === 'down' ? 'Retry' : 'Issue now'" color="neutral" variant="ghost" size="xs"
          :aria-label="`${cert.installed ? 'Renew' : 'Issue'} ${cert.name}`" @click="emit('renew', cert.name)"
        />
      </li>
    </ul>
    <p v-else-if="domain.dns_provider" class="px-4 py-2 text-sm text-muted">{{ entry.text }}</p>
  </section>
</template>
