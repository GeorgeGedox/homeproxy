<script setup lang="ts">
const open = defineModel<boolean>("open", { required: true });
const props = defineProps<{ crowdsec: ReturnType<typeof useDecisions> }>();
const form = reactive({ value: "", type: "ban" as "ban" | "captcha", duration_seconds: 86400 });
/** This dialog's own refusal, not an older error of the list. */
const refused = ref("");
const actions = [{ label: "Ban: block every request", value: "ban" }, { label: "Captcha: ask to prove they are human", value: "captcha" }];

watch(open, (now) => {
  // The refusal also landed on the list behind; reading the list again puts its own state back.
  if (!now && refused.value) void props.crowdsec.load();
  if (!now) return;
  Object.assign(form, { value: "", type: "ban", duration_seconds: 86400 });
  refused.value = "";
});

async function submit(): Promise<void> {
  const ok = await props.crowdsec.ban({ ...form, value: form.value.trim() });
  refused.value = ok ? "" : props.crowdsec.error.value;
  if (ok) open.value = false;
}
</script>

<template>
  <UModal v-model:open="open" title="Ban an address" description="CrowdSec blocks it on every public site until the time runs out">
    <template #body>
      <UForm id="ban-address" :state="form" class="space-y-4" @submit="submit">
        <UFormField label="Address or range" name="value" required>
          <UInput v-model="form.value" placeholder="203.0.113.7" class="w-full" />
        </UFormField>
        <UFormField label="Action" name="type">
          <USelect v-model="form.type" :items="actions" class="w-full" />
        </UFormField>
        <UFormField label="For" name="duration_seconds">
          <USelect v-model="form.duration_seconds" :items="DURATIONS" class="w-full" />
        </UFormField>
        <p v-if="refused" class="text-sm text-error">{{ refused }}</p>
      </UForm>
    </template>
    <template #footer>
      <UButton type="submit" form="ban-address" :label="form.type === 'ban' ? 'Ban' : 'Ask for a captcha'" />
      <UButton color="neutral" variant="ghost" label="Cancel" @click="open = false" />
    </template>
  </UModal>
</template>
