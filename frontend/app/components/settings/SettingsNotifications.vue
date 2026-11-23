<script setup lang="ts">
import type { components } from "~~/api/schema";

const notifications = defineModel<components["schemas"]["Notifications"]>({ required: true });
const { urls, put } = useNotificationUrls();
// Fill the defaults the backend leaves out, so the fields below always have something to bind to.
notifications.value.targets ??= [];
notifications.value.heartbeat ??= { enabled: false, interval: "1m" };
const targets = notifications.value.targets;
const heartbeat = notifications.value.heartbeat;
const clearError = ref("");
// A target's own key, since its index moves on a remove and its name is edited in place.
const keys = new WeakMap<object, number>();
let next = 0;
function keyOf(target: object): number {
  if (!keys.has(target)) keys.set(target, next++);
  return keys.get(target) as number;
}

const add = () => targets.push({ name: "", type: "discord", events: [...NOTIFICATION_EVENTS] });
const saveTarget = (name: string) => (value: string) => put({ targets: { [name]: value } });

async function clearUnused(name: string): Promise<void> {
  clearError.value = await put({ targets: { [name]: "" } });
}
</script>

<template>
  <SettingsSection id="notifications" title="Notifications" description="Targets and their events are staged. URLs are saved at once and never shown.">
    <SettingsNotificationTarget
      v-for="(target, index) in targets"
      :key="keyOf(target)"
      :model-value="target"
      :saved="target.name in (urls?.targets ?? {})"
      :is-set="urls?.targets[target.name] ?? false"
      :save="saveTarget(target.name)"
      @remove="targets.splice(index, 1)"
    />
    <UButton color="neutral" variant="outline" icon="i-lucide-plus" label="Add target" @click="add" />
    <UAlert v-for="name in urls?.unused ?? []" :key="name" color="warning" variant="subtle" :title="`A URL is left over for ${name}`" description="No target has this name any more.">
      <template #actions><UButton color="error" variant="subtle" size="xs" label="Clear" :aria-label="`Clear leftover URL of ${name}`" @click="clearUnused(name)" /></template>
    </UAlert>
    <UAlert v-if="clearError" color="error" variant="subtle" :description="clearError" />
    <USwitch v-model="heartbeat.enabled" label="Heartbeat" description="Ping a URL every interval while HAProxy answers, so an outside service alerts when the pings stop." />
    <UFormField label="Heartbeat interval" name="notifications.heartbeat.interval" hint="At least 30s, for example 1m"><UInput v-model="heartbeat.interval" class="w-full" /></UFormField>
    <SettingsSecretField label="Heartbeat URL" noun="URL" testid="url-Heartbeat URL" :is-set="urls?.heartbeat_url ?? false" :save="(value: string) => put({ heartbeat_url: value })" />
    <SettingsNotificationTestButton :disabled="!urls?.heartbeat_url" />
  </SettingsSection>
</template>
