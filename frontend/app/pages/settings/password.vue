<script setup lang="ts">
const api = useApi();
const form = reactive({ current: "", next: "", repeat: "" });
const mismatch = computed(() => form.repeat !== "" && form.repeat !== form.next);
const error = ref("");
const done = ref(false);

async function save(): Promise<void> {
  if (form.repeat !== form.next) return;
  error.value = "";
  done.value = false;
  const { response, error: failure } = await api.PUT("/api/v1/auth/password", {
    body: { current_password: form.current, new_password: form.next },
  });
  done.value = response.ok;
  if (response.ok) form.current = form.next = form.repeat = "";
  else error.value = apiErrorMessage(failure, "The password was not changed.");
}
</script>

<template>
  <UDashboardPanel id="password">
    <template #header>
      <UDashboardNavbar title="Settings" />
    </template>
    <template #body>
      <div class="flex flex-col gap-4 md:flex-row md:gap-8">
      <SettingsNav current="dashboard" />
      <SettingsSection title="Change password" description="Every other session is signed out; this one stays." class="min-w-0 max-w-xl flex-1">
        <form class="space-y-4" @submit.prevent="save">
          <UFormField label="Current password"><UInput v-model="form.current" type="password" autocomplete="current-password" required class="w-full" /></UFormField>
          <UFormField label="New password" hint="At least 10 characters"><UInput v-model="form.next" type="password" autocomplete="new-password" required class="w-full" /></UFormField>
          <UFormField label="Repeat the new password" :error="mismatch ? 'The passwords do not match.' : undefined">
            <UInput v-model="form.repeat" type="password" autocomplete="new-password" required class="w-full" />
          </UFormField>
          <UAlert v-if="error" color="error" variant="subtle" :description="error" />
          <UAlert v-if="done" color="success" variant="subtle" description="Password changed." />
          <div class="flex gap-2">
            <UButton type="submit" label="Change password" />
            <UButton to="/settings/dashboard" color="neutral" variant="ghost" label="Cancel" />
          </div>
        </form>
      </SettingsSection>
      </div>
    </template>
  </UDashboardPanel>
</template>
