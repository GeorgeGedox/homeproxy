<script setup lang="ts">
import type { NavigationMenuItem } from "@nuxt/ui";

const route = useRoute();
const session = useSession();
const pending = usePending();
const { data: status, refresh: refreshStatus } = useStatus();

const items = computed<NavigationMenuItem[][]>(() =>
  NAV_GROUPS.map((group) => [
    ...(group.label ? [{ label: group.label, type: "label" as const }] : []),
    ...group.items.map((item) => ({ label: item.label, icon: item.icon, to: item.to, active: isActive(item, route.path) })),
  ]),
);

const version = computed(() => haproxyVersion(status.value));

// useStatus fetches on mount by itself.
void pending.refresh();
watch(() => route.path, () => void Promise.all([pending.refresh(), refreshStatus()]));
</script>

<template>
  <UDashboardGroup unit="rem">
    <UDashboardSidebar
      :default-size="15" :min-size="15" :max-size="15"
      :ui="{ root: 'bg-muted', footer: 'border-t border-default' }"
    >
      <template #header>
        <NuxtLink to="/" aria-label="HomeProxy overview" class="rounded-md transition-opacity hover:opacity-80"><AppLogo /></NuxtLink>
      </template>

      <template #default>
        <UNavigationMenu
          :items="items" orientation="vertical" color="neutral" aria-label="Main"
          :ui="{ separator: 'hidden', label: 'mt-4 font-normal text-dimmed', link: 'rounded-md', linkLeadingIcon: 'size-4' }"
        />
      </template>

      <template #footer>
        <div class="flex w-full items-center gap-2">
          <NuxtLink to="/" class="-ml-2 flex h-8 min-w-0 flex-1 items-center gap-2 rounded-md px-2 text-xs text-muted transition-colors hover:bg-elevated hover:text-highlighted">
            <StatusLed :tone="version ? 'up' : 'down'" />
            <span class="truncate">HAProxy {{ version ?? "not answering" }}</span>
          </NuxtLink>
          <UTooltip text="Log out">
            <UButton icon="lucide:log-out" color="neutral" variant="ghost" size="sm" aria-label="Log out" @click="session.logout()" />
          </UTooltip>
        </div>
      </template>
    </UDashboardSidebar>

    <slot />

    <ChangesBar />
  </UDashboardGroup>
</template>
