<script setup lang="ts">
import type { components } from "~~/api/schema";

const props = defineProps<{ certificates: components["schemas"]["CertificateInfo"][] }>();

const TONES = { ok: "text-dimmed", warning: "text-warning", alert: "text-error" } as const;
const soonest = computed(() => [...props.certificates].sort((a, b) => a.days_left - b.days_left).slice(0, 5));
</script>

<template>
  <section aria-labelledby="certs-title" class="rounded-lg border border-default p-4">
    <h2 id="certs-title" class="flex items-center justify-between text-sm text-highlighted">
      Certificates
      <UButton to="/domains" label="All domains" color="neutral" variant="ghost" size="xs" class="-my-1 text-muted" />
    </h2>
    <p v-if="!certificates.length" class="mt-3 text-sm text-muted">None yet. A domain with a DNS provider gets one after its first apply.</p>
    <ul v-else class="mt-3 space-y-2 text-sm">
      <li v-for="cert in soonest" :key="cert.name" class="flex items-center gap-2">
        <span class="min-w-0 flex-1 truncate font-mono">{{ cert.names[0] ?? cert.name }}</span>
        <span class="font-mono text-xs tabular-nums" :class="TONES[cert.state]">{{ cert.days_left < 0 ? "expired" : `${cert.days_left}d left` }}</span>
      </li>
    </ul>
  </section>
</template>
