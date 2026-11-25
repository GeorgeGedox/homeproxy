<script setup lang="ts">
import type { components } from "~~/api/schema";
import type { LedTone } from "~/utils/health";

const props = defineProps<{
  zone: string;
  rows: CloudflareRow[];
  errors: string[];
  ddns?: components["schemas"]["DdnsResult"];
  /** Adopt failures by zone (the DDNS record) and by host (a site record). */
  adoptErrors: Record<string, string>;
  busy: boolean;
  /** Whether edits are staged: waiting changes then go out at the next apply. */
  staged: boolean;
  siteOf: (host: string) => string | undefined;
}>();
defineEmits<{ adopt: []; adoptHost: [string] }>();

const when = computed(() => (props.staged ? "at the next apply" : "at the next sync"));
const words = computed(() => ({ live: "", adding: `added ${when.value}`, removing: `removed ${when.value}`, changing: `changed ${when.value}`, "hand-made": "" }));
/** Site records in Cloudflare now: not the ones only waiting to be added, nor hand-made ones. */
const siteCount = computed(() => props.rows.filter((row) => row.state !== "hand-made" && row.state !== "adding").length);

/** Each site record's light: in Cloudflare, waiting for a change, or hand-made. */
const tone: Record<CloudflareRow["state"], LedTone> = { live: "up", adding: "staged", removing: "staged", changing: "staged", "hand-made": "unknown" };
const current = { created: "Created with your address", updated: "Moved to your address", adopted: "Adopted, and holds your address", unchanged: "Holds your address" };
/** The DDNS record's light and what it says. */
const ddnsState = computed((): { tone: LedTone; text: string } | undefined => {
  const action = props.ddns?.action;
  if (!action) return undefined;
  if (action === "hand-made") return { tone: "unknown", text: "Made by hand, so HomeProxy leaves it. Adopt it to keep it at your address" };
  if (action === "blocked" || action === "failed") return { tone: "down", text: props.ddns?.error || "The update failed" };
  return { tone: "up", text: current[action] };
});
</script>

<template>
  <section class="rounded-lg border border-default" :aria-label="zone">
    <header class="flex items-center gap-3 rounded-t-lg border-b border-default bg-muted px-4 py-3">
      <div class="min-w-0 flex-1">
        <h2 class="truncate font-mono text-sm text-highlighted">{{ zone }}</h2>
        <p class="text-xs text-muted">{{ siteCount === 1 ? "1 site record" : `${siteCount || "No"} site records` }}</p>
      </div>
    </header>
    <ul class="divide-y divide-default">
      <li v-for="error in errors" :key="error" class="px-4 py-2 text-sm text-error">{{ error }}</li>
      <li v-if="ddns && ddnsState" class="flex flex-wrap items-center gap-x-3 gap-y-1 px-4 py-2 text-sm" :aria-label="`DDNS ${ddns.record}`">
        <StatusLed :tone="ddnsState.tone" :label="ddnsState.text" />
        <span class="min-w-0 truncate font-mono text-highlighted">{{ ddns.record }}</span>
        <span v-if="ddns.content" class="font-mono text-muted"><span class="text-dimmed">→ </span>{{ ddns.content }}</span>
        <span class="flex-1" />
        <UButton v-if="ddns.action === 'hand-made'" label="Adopt" color="neutral" variant="outline" size="xs" :loading="busy" @click="$emit('adopt')" />
        <p class="basis-full pl-5 text-xs" :class="ddnsState.tone === 'down' ? 'text-error' : 'text-muted'">DDNS · {{ ddnsState.text }}</p>
        <p v-if="adoptErrors[zone]" class="basis-full pl-5 text-xs text-error">{{ adoptErrors[zone] }}</p>
      </li>
      <li v-for="row in rows" :key="row.name" :class="row.state === 'removing' && 'opacity-60'">
        <!-- A record of a site opens the site's editor; the whole row is the link. -->
        <ULink
          raw as="div" :to="siteOf(row.name) ? { path: '/routes/edit', query: { name: siteOf(row.name) } } : undefined"
          class="flex flex-wrap items-center gap-x-3 gap-y-1 px-4 py-2 text-sm" :class="siteOf(row.name) && 'group transition-colors [--focus-offset:-2px] hover:bg-elevated'"
        >
          <StatusLed :tone="tone[row.state]" :label="row.state === 'hand-made' ? 'Made by hand' : words[row.state] || 'In Cloudflare'" />
          <span class="min-w-0 truncate font-mono text-highlighted">{{ row.name }}</span>
          <template v-if="row.state !== 'hand-made'">
            <span class="font-mono text-muted"><span class="text-dimmed">→ </span>{{ row.content }}</span>
            <span v-if="row.was" class="font-mono text-xs text-dimmed">was {{ row.was }}</span>
            <span v-if="words[row.state]" class="text-xs text-primary">{{ words[row.state] }}</span>
            <span class="flex-1" />
            <span class="text-xs text-muted">{{ row.proxied ? "Proxied" : "DNS only" }}</span>
          </template>
          <template v-else>
            <span class="flex-1" />
            <!-- The row is a link to the site's editor; the button must not follow it. -->
            <UButton label="Adopt" color="neutral" variant="outline" size="xs" :loading="busy" @click.stop.prevent="$emit('adoptHost', row.name)" />
          </template>
          <UIcon v-if="siteOf(row.name)" name="lucide:chevron-right" class="size-4 shrink-0 text-dimmed group-hover:text-highlighted" />
          <p v-if="row.state === 'hand-made'" class="basis-full pl-5 text-xs text-muted">
            Made by hand in Cloudflare, so HomeProxy leaves it. Adopt it to hand it to its site.
          </p>
          <p v-if="adoptErrors[row.name]" class="basis-full pl-5 text-xs text-error">{{ adoptErrors[row.name] }}</p>
        </ULink>
      </li>
      <li v-if="!rows.length" class="px-4 py-2 text-sm text-muted">
        No site records yet. A public site on this domain gets one when you turn on its Cloudflare record.
      </li>
    </ul>
  </section>
</template>
