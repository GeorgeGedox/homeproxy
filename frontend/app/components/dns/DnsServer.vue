<script setup lang="ts">
import type { LedTone } from "~/utils/health";

defineProps<{
  state: { tone: LedTone; text: string };
  listen: string;
  forwardTo: string[];
  forwardStaged: string[] | null;
  zones: string[];
  adguardLines: string;
}>();
defineEmits<{ copy: [] }>();
</script>

<template>
  <section class="rounded-lg border border-default" aria-label="DNS server">
    <header class="flex items-center gap-3 rounded-t-lg border-b border-default bg-muted px-4 py-3">
      <UTooltip :text="state.text"><StatusLed :tone="state.tone" :label="state.text" /></UTooltip>
      <div class="min-w-0 flex-1">
        <h2 class="text-sm text-highlighted">DNS server <span class="text-muted">· {{ state.text }}</span></h2>
        <p class="text-xs text-muted">
          Answers on <span class="font-mono">{{ listen }}</span> for {{ zones.length ? zones.join(", ") : "your domains" }}
        </p>
      </div>
    </header>
    <div class="space-y-3 px-4 py-3 text-sm">
      <p v-if="forwardTo.length" class="text-muted">
        Other names in these zones go to <span class="font-mono text-default">{{ forwardTo.join(", ") }}</span>
      </p>
      <p v-else class="flex items-start gap-2 text-muted">
        <UIcon name="lucide:triangle-alert" class="mt-0.5 size-4 shrink-0 text-warning" />
        <span>Other names in these zones fail, because no upstream resolver is set. <ULink to="/settings#dns" class="text-highlighted underline">Add one in Settings</ULink></span>
      </p>
      <p v-if="forwardStaged" class="text-xs text-primary">
        Staged: {{ forwardStaged.length ? `forward to ${forwardStaged.join(", ")}` : "forward to no resolver" }}
      </p>
      <div v-if="zones.length" class="space-y-2">
        <p class="text-muted">In AdGuard Home, add these upstream DNS servers, so your zones go to HomeProxy and everything else stays as it is:</p>
        <div class="flex flex-col items-start gap-2 rounded-md border border-default bg-muted px-3 py-2 sm:flex-row">
          <pre class="w-full min-w-0 flex-1 font-mono text-xs break-all whitespace-pre-wrap text-highlighted">{{ adguardLines }}</pre>
          <UButton icon="lucide:copy" label="Copy" color="neutral" variant="ghost" size="xs" @click="$emit('copy')" />
        </div>
      </div>
    </div>
  </section>
</template>
