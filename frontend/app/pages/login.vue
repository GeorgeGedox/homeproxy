<script setup lang="ts">
definePageMeta({ layout: "blank" });

const api = useApi();
const password = ref("");
const error = ref("");
const busy = ref(false);

async function submit(): Promise<void> {
  busy.value = true;
  error.value = "";
  const { response, error: failure } = await api.POST("/api/v1/auth/login", { body: { password: password.value } });
  busy.value = false;
  if (response.ok) {
    password.value = "";
    await navigateTo("/");
  } else {
    error.value = response.status === 401 ? "Wrong password." : apiErrorMessage(failure, "Login failed.");
  }
}
</script>

<template>
  <form class="space-y-4" @submit.prevent="submit">
    <UFormField label="Password" name="password">
      <UInput v-model="password" type="password" autocomplete="current-password" required class="w-full" />
    </UFormField>
    <UAlert v-if="error" color="error" variant="subtle" :description="error" />
    <UButton type="submit" block :loading="busy" label="Log in" />
  </form>
</template>
