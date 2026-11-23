<script setup lang="ts">
const props = defineProps<{
  /** The other paths of the site, which the host settings here also change. */
  others: Route[];
  /** Whether staging changes the host settings of those paths. */
  changed: boolean;
  lanIp: string;
  cloudflareAvailable: boolean;
}>();
const draft = defineModel<RouteDraft>({ required: true });
const isTcp = computed(() => draft.value.type === "tcp");
const site = computed(() => draft.value.hosts.join(", ") || "this site");
// The other paths of one site share its host, so their paths name them.
const otherLabels = computed(() => props.others.map((route) => (route.type === "http" ? route.path_prefix : routeTarget(route))).sort().join(", "));

const exposures = [
  { label: "LAN only", description: "Devices on your network", value: "lan" },
  { label: "Public", description: "Anyone on the internet", value: "public" },
];
const dnsModes = computed(() => [
  { label: `HomeProxy${props.lanIp ? ` (${props.lanIp})` : ""}`, description: "LAN devices go through the proxy", value: "haproxy" },
  { label: "Another address", description: "LAN devices go straight to it", value: "other" },
]);
const dnsMode = computed({
  get: () => (draft.value.dns_target === "haproxy" ? "haproxy" : "other"),
  set: (mode: string) => (draft.value.dns_target = mode === "haproxy" ? "haproxy" : ""),
});
const proxyModes = { default: null, on: true, off: false } as const;
const proxied = computed({
  // Passthrough has no zone default and saves an unset choice as DNS only.
  get: () => (draft.value.cloudflare_proxied === null ? (draft.value.type === "passthrough" ? "off" : "default") : draft.value.cloudflare_proxied ? "on" : "off"),
  set: (mode: keyof typeof proxyModes) => (draft.value.cloudflare_proxied = proxyModes[mode]),
});
const proxiedItems = computed(() => [
  ...(draft.value.type === "passthrough" ? [] : [{ label: "Zone default", value: "default" }]),
  { label: "Proxied through Cloudflare", value: "on" },
  { label: "DNS only", value: "off" },
]);
const cards = { fieldset: "grid grid-cols-1 gap-2 sm:grid-cols-2", legend: "sr-only" };
</script>

<template>
  <section class="space-y-4 border-t border-default pt-6">
    <div v-if="isTcp">
      <h2 class="text-base font-medium text-highlighted">Who can reach it</h2>
    </div>
    <div v-else class="space-y-1">
      <h2 class="text-base font-medium text-highlighted">Site <span v-if="draft.hosts.length" class="font-mono text-sm">{{ site }}</span></h2>
      <p class="text-sm text-muted">
        <template v-if="others.length">Applies to every path of the site: this one and {{ otherLabels }}</template>
        <template v-else-if="draft.type === 'http'">Applies to this route and to any path you add to the site later</template>
      </p>
    </div>

    <UFormField :label="isTcp ? undefined : 'Who can reach it'" name="exposure">
      <URadioGroup v-model="draft.exposure" :items="exposures" variant="card" color="neutral" legend="Who can reach it" :ui="{ ...cards, fieldset: 'grid grid-cols-2 gap-2' }" />
    </UFormField>

    <template v-if="!isTcp">
      <!-- No name: the Address field below shows the target's error, which only "Another address" can have. -->
      <UFormField label="On your network" :description="`What the internal DNS server answers for ${site}`">
        <URadioGroup v-model="dnsMode" :items="dnsModes" variant="card" color="neutral" legend="On your network" :ui="cards" />
      </UFormField>
      <UFormField v-if="dnsMode === 'other'" label="Address" name="dns.target" description="An IPv4 address on your network">
        <UInput v-model="draft.dns_target" placeholder="192.168.1.30" class="w-full" />
      </UFormField>
      <template v-if="cloudflareAvailable && draft.exposure === 'public'">
        <USwitch v-model="draft.cloudflare_dns" label="Cloudflare DNS record" :description="`Created for ${site} at the next apply`" />
        <UFormField v-if="draft.cloudflare_dns" label="Cloudflare proxy" name="cloudflare.proxied">
          <USelect v-model="proxied" :items="proxiedItems" class="w-full" />
        </UFormField>
      </template>
      <p v-if="changed" class="flex items-start gap-2 text-sm text-primary">
        <UIcon name="lucide:info" class="mt-0.5 size-4 shrink-0" />Stage change also updates {{ otherLabels }}
      </p>
    </template>
  </section>
</template>
