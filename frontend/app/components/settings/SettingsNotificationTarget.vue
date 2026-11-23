<script setup lang="ts">
import type { components } from "~~/api/schema";

const target = defineModel<components["schemas"]["NotificationTarget"]>({ required: true });
defineProps<{
  /** False until the target is saved in the settings: its URL can only be set after that. */
  saved: boolean;
  isSet: boolean;
  save: (value: string) => Promise<string>;
}>();
defineEmits<{ remove: [] }>();
</script>

<template>
  <UCard :ui="{ body: 'space-y-3' }">
    <div class="grid gap-3 sm:grid-cols-[1fr_auto]">
      <UFormField label="Target name"><UInput v-model="target.name" class="w-full" /></UFormField>
      <UFormField label="Target type"><USelect v-model="target.type" :items="['discord', 'webhook']" class="w-full" /></UFormField>
    </div>
    <UFormField label="Events">
      <USelectMenu v-model="target.events" multiple :items="NOTIFICATION_EVENTS" :aria-label="`Events of ${target.name}`" class="w-full" />
    </UFormField>
    <template v-if="saved">
      <SettingsSecretField :label="`URL of ${target.name}`" noun="URL" :testid="`url-URL of ${target.name}`" :is-set="isSet" :save="save" />
      <SettingsNotificationTestButton :target="target.name" :disabled="!isSet" />
    </template>
    <p v-else class="text-sm text-muted">Save the settings to set this target's URL.</p>
    <UButton color="error" variant="ghost" size="xs" label="Remove target" :aria-label="`Remove ${target.name}`" @click="$emit('remove')" />
  </UCard>
</template>
