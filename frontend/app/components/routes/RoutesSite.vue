<script setup lang="ts">
import type { DropdownMenuItem } from "@nuxt/ui";
import type { RouteState } from "~/composables/useSites";

const props = defineProps<{
  site: Site;
  state: RouteState;
  routeState: (route: Route) => RouteState;
  staged: Set<string | undefined>;
  /** Cloudflare is on and manages this site's domain, so its DNS switch can work. */
  cloudflareOn: boolean;
  /** Why a host of this site may not answer, from the backend, shown under the paths. */
  warnings: { host: string; message: string }[];
}>();
const emit = defineEmits<{ exposure: [boolean]; cloudflare: [boolean]; enabled: [Route, boolean]; remove: [Route] }>();

const first = computed(() => props.site.routes[0] as Route);
const isPublic = computed(() => first.value.exposure === "public");
const isHttp = computed(() => first.value.type === "http");
// Only a public site on a Cloudflare domain gets a record, so the switch shows only there.
const showCloudflare = computed(() => props.cloudflareOn && isPublic.value && first.value.type !== "tcp");
const exposures = [{ label: "LAN only", public: false }, { label: "Public", public: true }];
const cloudflareDns = computed(() => first.value.type !== "tcp" && Boolean(first.value.cloudflare?.dns));
const title = computed(() => (first.value.type === "tcp" ? `Port ${first.value.listen_port}` : props.site.hosts.join(", ")));

const editLink = (route: Route) => ({ path: "/routes/edit", query: { name: route.name } });
const menu = (route: Route): DropdownMenuItem[] => [
  { label: "Edit", icon: "lucide:pencil", to: editLink(route) },
  { label: "Duplicate", icon: "lucide:copy", to: { path: "/routes/edit", query: { copy: route.name } } },
  { type: "separator" },
  { label: "Delete", icon: "lucide:trash-2", color: "error", onSelect: () => emit("remove", route) },
];

/** Where a route sends its traffic, in the words of its type. */
function target(route: Route): { lead: string; to: string } {
  if (route.type === "redirect") return { lead: "Redirect", to: `${route.redirect.to} (${route.redirect.code})` };
  const upstreams = route.upstreams.map((upstream) => upstream.address);
  const to = upstreams.length > 1 ? `${upstreams[0]} +${upstreams.length - 1}` : (upstreams[0] ?? "");
  if (route.type === "passthrough") return { lead: "TLS passthrough", to };
  if (route.type === "tcp") return { lead: "TCP", to };
  return { lead: route.path_prefix, to };
}
</script>

<template>
  <section class="rounded-lg border border-default" :aria-label="title">
    <header class="flex flex-wrap items-center gap-x-4 gap-y-2 rounded-t-lg border-b border-default bg-muted px-4 py-3">
      <div class="flex min-w-0 flex-1 basis-full items-center gap-3 sm:basis-0">
        <UTooltip :text="state.text">
          <StatusLed :tone="state.tone" :label="state.text" />
        </UTooltip>
        <h3 class="min-w-0 truncate font-mono text-sm text-highlighted">{{ title }}</h3>
      </div>
      <div class="flex w-full items-center justify-end gap-4 sm:w-auto">
        <USwitch
          v-if="showCloudflare" :model-value="cloudflareDns" size="lg" label="Cloudflare DNS" :aria-label="`Cloudflare DNS record for ${title}`"
          :ui="{ label: 'text-muted' }" class="mr-auto sm:mr-0" @update:model-value="emit('cloudflare', $event)"
        />
        <UFieldGroup size="sm" role="group" :aria-label="`Who can reach ${title}`">
          <UButton
            v-for="option in exposures" :key="option.label" :label="option.label" color="neutral"
            :variant="option.public === isPublic ? 'subtle' : 'outline'" :class="option.public === isPublic ? 'text-highlighted' : 'text-dimmed'" :aria-pressed="option.public === isPublic"
            @click="option.public !== isPublic && emit('exposure', option.public)"
          />
        </UFieldGroup>
      </div>
    </header>

    <ul class="divide-y divide-default">
      <li v-for="route in site.routes" :key="route.name" class="flex items-center gap-3 px-4 py-2">
        <UTooltip :text="routeState(route).text">
          <StatusLed :tone="routeState(route).tone" :label="routeState(route).text" />
        </UTooltip>
        <NuxtLink
          :to="editLink(route)" class="-my-1 flex min-w-0 flex-1 flex-wrap items-baseline gap-x-3 rounded-md py-1 text-sm transition-colors hover:text-highlighted"
          :class="!route.enabled && 'opacity-50'"
        >
          <span class="font-mono text-highlighted">{{ target(route).lead }}</span>
          <span class="min-w-0 truncate font-mono text-muted"><span class="text-dimmed">→ </span>{{ target(route).to }}</span>
          <span v-if="onlyOn(route, site).length" class="text-xs text-dimmed">on {{ onlyOn(route, site).join(", ") }}</span>
          <span v-if="staged.has(route.name)" class="text-xs text-primary">staged</span>
        </NuxtLink>
        <USwitch
          :model-value="route.enabled" size="lg" :label="route.enabled ? 'On' : 'Off'" :aria-label="`Enabled ${routeTarget(route)}`"
          :ui="{ label: 'w-6 text-muted' }" @update:model-value="emit('enabled', route, $event)"
        />
        <UDropdownMenu :items="menu(route)" :content="{ align: 'end' }">
          <UButton icon="lucide:ellipsis" color="neutral" variant="ghost" size="sm" :aria-label="`Actions for ${routeTarget(route)}`" />
        </UDropdownMenu>
      </li>
      <li v-if="isHttp" class="px-1.5 py-1">
        <UButton :to="{ path: '/routes/edit', query: { host: site.hosts[0] } }" label="Add path" icon="lucide:plus" color="neutral" variant="ghost" size="sm" class="text-muted" />
      </li>
    </ul>

    <div v-for="warning in warnings" :key="warning.host" class="flex items-start gap-3 border-t border-default px-4 py-2 text-sm text-muted">
      <UIcon name="lucide:triangle-alert" class="mt-0.5 size-4 shrink-0 text-warning" />
      <span class="min-w-0 flex-1">{{ warning.message }}</span>
      <UButton :to="{ path: '/routes/edit', query: { host: warning.host, path: '/' } }" label="Add /" color="neutral" variant="outline" size="xs" />
    </div>
  </section>
</template>
