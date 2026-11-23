<script setup lang="ts">
/** Staged edits block a save of these settings, so the button waits for them. */
defineProps<{ blocked?: boolean }>();
const { form, view, pendingListen, secondsLeft, listenChanged, listenError, error, saved, reverted, busy, load, save } = useDashboardSettings();
onMounted(() => void load());
</script>

<template>
  <UAlert v-if="!view && error" color="error" variant="subtle" :description="error" />
  <SettingsSection v-else-if="view" title="Dashboard" description="Where this dashboard listens and how long a login lasts.">
    <UFormField label="Dashboard address" hint="ip:port" :error="listenError || undefined">
      <UInput v-model="form.listen" class="w-full" />
    </UFormField>
    <UAlert
      v-if="listenChanged"
      color="warning"
      variant="subtle"
      title="The dashboard moves to the new address"
      description="It opens on the new address at once. Open it there and sign in within 60 seconds, or the change reverts and the old address stays. A new IP may not accept this session, so you may need to sign in again there."
    />
    <UFormField label="Session length" help="For example 12h or 7d, at least 5m. A change applies at once and keeps this session.">
      <UInput v-model="form.session_ttl" class="w-full" />
    </UFormField>
    <UAlert v-if="error" color="error" variant="subtle" :description="error" />
    <UAlert v-if="pendingListen && secondsLeft > 0" color="info" variant="subtle" title="Confirm the new address" data-testid="listen-pending">
      <template #description>
        <p>
          The dashboard now also listens on {{ pendingListen.listen }}. Open it and sign in within {{ secondsLeft }} s, or the change reverts.
          You may need to sign in again there.
        </p>
        <UButton class="mt-2" :to="pendingListen.url" external target="_blank" icon="i-lucide-external-link" label="Open the new address" />
      </template>
    </UAlert>
    <UAlert
      v-else-if="reverted"
      color="warning"
      variant="subtle"
      :description="`Nobody signed in on ${reverted} within 60 seconds, so the address change was reverted.`"
      data-testid="listen-reverted"
    />
    <UAlert v-else-if="saved" color="success" variant="subtle" description="Saved." />
    <UButton :loading="busy" :disabled="blocked" label="Save" @click="save" />
  </SettingsSection>
</template>
