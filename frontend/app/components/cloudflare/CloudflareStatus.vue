<script setup lang="ts">
import type { LedTone } from "~/utils/health";

const props = defineProps<{
  status: { tone: LedTone; text: string };
  state: CloudflareState;
  interval: string;
  error: string;
  /** Whether edits are staged, so waiting record changes go out at the next apply rather than at a sync. */
  staged: boolean;
}>();

/** When records were last synced and DDNS last checked, and how often the sync runs. */
const facts = computed(() => {
  const every = props.interval ? [`every ${props.interval}`] : [];
  const last = props.state.last_reconcile;
  const synced = last ? [`${last.error ? "Sync tried" : "Records synced"} ${timeAgo(last.at)}`, ...every] : props.interval ? [`Records sync every ${props.interval}`] : [];
  const checked = props.state.ddns_checked_at ? [`DDNS checked ${timeAgo(props.state.ddns_checked_at)}`] : [];
  return [...synced, ...checked];
});
const failure = computed(() => props.error || props.state.last_reconcile?.error || "");
</script>

<template>
  <section class="rounded-lg border border-default" aria-label="Cloudflare sync">
    <header class="flex items-center gap-3 rounded-t-lg bg-muted px-4 py-3" :class="!state.enabled || failure || status.tone === 'staged' ? 'border-b border-default' : 'rounded-b-lg'">
      <UTooltip :text="status.text"><StatusLed :tone="status.tone" :label="status.text" /></UTooltip>
      <div class="min-w-0 flex-1">
        <h2 class="text-sm text-highlighted">Cloudflare DNS <span class="text-muted">· {{ status.text }}</span></h2>
        <p v-if="state.enabled" class="text-xs text-muted">{{ facts.join(" · ") }}</p>
      </div>
    </header>
    <p v-if="failure" class="px-4 py-3 text-sm text-error">{{ failure }}</p>
    <p v-if="!state.enabled" class="px-4 py-3 text-sm text-muted">
      HomeProxy leaves your Cloudflare DNS records as they are. <ULink to="/settings#cloudflare" class="text-highlighted underline">Turn it on in Settings</ULink>
    </p>
    <p v-else-if="status.tone === 'staged' && !failure" class="px-4 py-3 text-sm text-muted">
      {{ staged ? "They go out to Cloudflare at the next apply." : "Records in Cloudflare differ from your sites. Sync now puts them back." }}
    </p>
  </section>
</template>
