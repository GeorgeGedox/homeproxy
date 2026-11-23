<script setup lang="ts">
import type { LedTone } from "~/utils/health";
import type { LogLine } from "~/composables/useLogTail";

/** The size of the stored access log, for the download. */
defineProps<{ size?: number }>();
const tail = useLogTail();
const filters = tail.filters;
/** The line shown in full, as HAProxy wrote it. */
const opened = ref<string>();
const api = useApi();
const { data: routes } = useAsyncData("log-routes", async () => (await api.GET("/api/v1/routes")).data ?? []);
// The stream filters by the route's internal name; the admin picks the site it serves.
const ALL = "all";
const sites = computed(() => [{ label: "All sites", value: ALL }, ...(routes.value ?? []).map((route) => ({ label: routeTarget(route), value: route.name }))]);
const site = computed({ get: () => filters.route || ALL, set: (value: string) => (filters.route = value === ALL ? "" : value) });
const siteOf = (line: LogLine) => {
  const route = routes.value?.find((item) => item.name === line.route);
  return route ? routeTarget(route) : line.route ? `${line.route} (removed)` : "No site";
};
// "GET /a HTTP/1.1": the protocol is the same on almost every line.
const request = (line: LogLine) => line.request?.replace(/ HTTP\/[\d.]+$/, "") ?? (line.sni ? `TLS to ${line.sni}` : "TCP connection");

const light = computed((): { tone: LedTone; text: string } => {
  if (tail.refused.value) return { tone: "down", text: "Stopped" };
  if (tail.paused.value) return { tone: "staged", text: `Paused · ${formatCount(tail.held.value.length)} new` };
  return tail.connected.value ? { tone: "up", text: "Live" } : { tone: "unknown", text: "Reconnecting" };
});
const filtered = computed(() => Object.values(filters).some((value) => value.trim()));
const statusClass = (status: number) => (status >= 500 ? "text-error" : status >= 400 ? "text-warning" : "text-muted");
const time = (iso: string) => new Date(iso).toLocaleTimeString("en-GB");
</script>

<template>
  <section class="rounded-lg border border-default" aria-label="Requests">
    <header class="flex flex-wrap items-center gap-2 rounded-t-lg border-b border-default bg-muted px-4 py-3">
      <USelect v-model="site" :items="sites" aria-label="Site" size="sm" class="w-full sm:w-56" />
      <UInput v-model.lazy="filters.status" placeholder="Status" aria-label="Status" size="sm" class="w-24" />
      <UInput v-model.lazy="filters.client" placeholder="Client IP or range" aria-label="Client" size="sm" class="w-44" />
      <UInput v-model.lazy="filters.country" placeholder="Country" aria-label="Country" size="sm" class="w-24" />
      <UButton v-if="filtered" label="Clear" color="neutral" variant="link" size="sm" @click="tail.clear" />
      <span class="flex-1" />
      <span class="flex items-center gap-2 text-xs text-muted"><StatusLed :tone="light.tone" />{{ light.text }}</span>
      <UButton
        :label="tail.paused.value ? 'Resume' : 'Pause'" color="neutral" variant="outline" size="sm"
        @click="tail.paused.value ? tail.resume() : (tail.paused.value = true)"
      />
      <UButton
        to="/api/v1/logs/files/access" external download :label="size === undefined ? 'Download' : `Download · ${formatBytes(size)}`" color="neutral" variant="ghost" size="sm"
      />
    </header>
    <p v-if="tail.refused.value" class="border-b border-default px-4 py-2 text-sm text-error">{{ tail.refused.value }}</p>
    <ul v-if="tail.lines.value.length" class="divide-y divide-default font-mono text-xs" aria-label="Access lines">
      <li v-for="line in tail.lines.value" :key="tail.key(line)">
        <button
          type="button" :aria-expanded="opened === tail.key(line)" class="grid w-full grid-cols-[auto_auto_1fr] gap-x-3 gap-y-0.5 px-4 py-1.5 text-left hover:bg-elevated/50 md:grid-cols-[5rem_2.5rem_minmax(0,1fr)_14rem_9rem_2rem]"
          @click="opened = opened === tail.key(line) ? undefined : tail.key(line)"
        >
        <span class="text-dimmed">{{ time(line.logged_at) }}</span>
        <span :class="line.status ? statusClass(line.status) : 'text-dimmed'">{{ line.status ?? "TCP" }}</span>
        <span class="min-w-0 truncate text-highlighted">{{ request(line) }}</span>
        <span class="col-span-3 min-w-0 truncate text-muted md:col-span-1">
          {{ siteOf(line) }}<span class="md:hidden"> · {{ line.client_ip }}<template v-if="line.country"> · {{ line.country }}</template></span>
        </span>
        <span class="hidden truncate text-muted md:block">{{ line.client_ip }}</span>
        <span class="hidden text-muted md:block">{{ line.country ?? "" }}</span>
        </button>
        <pre v-if="opened === tail.key(line)" class="px-4 pb-2 whitespace-pre-wrap text-muted [overflow-wrap:anywhere]">{{ new Date(line.logged_at).toLocaleString("en-GB", { dateStyle: "medium", timeStyle: "medium" }) }}&#10;{{ line.line }}</pre>
      </li>
    </ul>
    <p v-else-if="!tail.refused.value" class="px-4 py-6 text-center text-sm text-muted">
      {{ filtered ? "No request matches these filters yet." : "No requests yet. They show here as they arrive." }}
    </p>
  </section>
</template>
