<script setup lang="ts">
import type { components } from "~~/api/schema";
import type { LedTone } from "~/utils/health";

const props = defineProps<{ status: components["schemas"]["Status"]; bans?: string }>();

interface Cell { label: string; tone: LedTone; value: string; meta?: string; warn?: boolean; to?: string }

const monthYear = (iso: string) => new Date(iso).toLocaleDateString("en", { month: "short", year: "numeric" });

function haproxy(): Cell {
  const s = props.status;
  const version = haproxyVersion(s);
  if (!version) return { label: "HAProxy", tone: "down", value: "Not answering" };
  const check = s.version_check;
  const value = `${version}${s.haproxy_uptime_seconds === null ? "" : ` · up ${duration(s.haproxy_uptime_seconds)}`}`;
  const eol = check?.eol ? `LTS until ${monthYear(check.eol)}` : undefined;
  if (check?.update_available) return { label: "HAProxy", tone: "up", value, meta: `${check.latest} available${eol ? ` · ${eol}` : ""}`, warn: true };
  return { label: "HAProxy", tone: "up", value, meta: eol };
}

function homeproxy(): Cell {
  const update = props.status.update_check;
  if (!props.status.update_check_configured) return { label: "HomeProxy", tone: "off", value: update?.running ?? "Running", meta: "Update check not set up" };
  // Set up, but no check has succeeded yet: the first runs at start, and a failed one keeps nothing.
  if (!update) return { label: "HomeProxy", tone: "unknown", value: "Running", meta: "Not checked yet" };
  if (update.update_available) return { label: "HomeProxy", tone: "up", value: update.running, meta: `${update.latest} available`, warn: true };
  return { label: "HomeProxy", tone: "up", value: update.running, meta: "Up to date" };
}

function lastApply(): Cell {
  const last = props.status.last_apply;
  if (!last) return { label: "Last apply", tone: "off", value: "Nothing applied yet" };
  if (!last.success) return { label: "Last apply", tone: "down", value: `Failed at ${last.stage}`, meta: timeAgo(last.time) };
  if (last.warnings.length) return { label: "Last apply", tone: "unknown", value: timeAgo(last.time), to: "/history", meta: "With warnings" };
  return { label: "Last apply", tone: "up", value: timeAgo(last.time), to: "/history", meta: "History" };
}

/** `bouncer` is null when CrowdSec is off, and also when HAProxy did not answer to say. */
function crowdsec(): Cell {
  const { bouncer, haproxy_error } = props.status;
  if (bouncer === null && haproxy_error) return { label: "CrowdSec", tone: "unknown", value: "Unknown", meta: "HAProxy is not answering", to: "/security" };
  if (bouncer === null) return { label: "CrowdSec", tone: "off", value: "Off", meta: "Turn on in Settings", to: "/settings#crowdsec" };
  return { label: "CrowdSec", tone: bouncer === "up" ? "up" : "down", value: `Bouncer ${bouncer}`, meta: props.bans, to: "/security" };
}

const cells = computed((): Cell[] => [
  haproxy(),
  props.status.dns_listening
    ? { label: "DNS server", tone: "up", value: props.status.dns_uptime_seconds === null ? "Answering" : `Up ${duration(props.status.dns_uptime_seconds)}`, to: "/dns" }
    : { label: "DNS server", tone: "down", value: "Not answering", to: "/dns" },
  crowdsec(),
  homeproxy(),
  lastApply(),
]);
</script>

<template>
  <!-- The 1px gap over a border-colored backdrop draws the hairlines between cells. -->
  <div class="grid shrink-0 grid-cols-2 gap-px overflow-hidden rounded-lg border border-default bg-(--ui-border) sm:grid-cols-3 xl:grid-cols-5">
    <ULink
      v-for="cell in cells" :key="cell.label" raw as="div" :to="cell.to"
      class="flex min-w-0 flex-col gap-1 bg-muted p-4 last:col-span-2 xl:last:col-span-1" :class="cell.to && 'transition-colors [--focus-offset:-2px] hover:bg-accented'"
    >
      <span class="flex items-center gap-2 text-xs text-muted"><StatusLed :tone="cell.tone" />{{ cell.label }}</span>
      <span class="truncate font-mono text-sm text-highlighted tabular-nums">{{ cell.value }}</span>
      <span v-if="cell.meta" class="line-clamp-2 text-xs" :class="cell.warn ? 'text-warning' : 'text-dimmed'">{{ cell.meta }}</span>
    </ULink>
  </div>
</template>
