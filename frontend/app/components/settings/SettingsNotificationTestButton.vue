<script setup lang="ts">
const props = defineProps<{ disabled?: boolean; target?: string }>();
const api = useApi();
const busy = ref(false);
const message = ref("");
const ok = ref(false);
// What a refusal usually means, so the admin knows what to fix.
const STATUS_HINTS: Record<number, string> = {
  400: " It did not accept the message: check that the target type matches the receiver.",
  401: " It refused the token in the URL: copy the URL again.",
  403: " It refused the token in the URL: copy the URL again.",
  404: " It has no webhook at this URL: check the URL, or create the webhook again and copy its new URL.",
};
// The heartbeat pings a monitoring service, which has a check, not a webhook or a target type.
const HEARTBEAT_HINTS: Record<number, string> = {
  400: " It did not accept the ping: copy the ping URL again from the monitoring service.",
  404: " It has no check at this URL: copy the ping URL again from the monitoring service.",
};

function hint(status: number | null): string {
  const code = status ?? 0;
  return (props.target ? undefined : HEARTBEAT_HINTS[code]) ?? STATUS_HINTS[code] ?? "";
}

async function run(): Promise<void> {
  busy.value = true;
  message.value = "";
  const { data, error } = props.target
    ? await api.POST("/api/v1/notifications/targets/{name}/test", { params: { path: { name: props.target } } })
    : await api.POST("/api/v1/notifications/heartbeat/test");
  busy.value = false;
  ok.value = !!data && data.error === null && data.status !== null && data.status < 300;
  message.value = data ? (data.error ?? `The receiver answered ${data.status}.${hint(data.status)}`) : apiErrorMessage(error, "The test was not sent.");
}
</script>

<template>
  <div class="flex flex-wrap items-center gap-2">
    <UButton color="neutral" variant="outline" size="sm" :loading="busy" :disabled="disabled" label="Test" :aria-label="`Test ${target ?? 'heartbeat'}`" @click="run" />
    <span v-if="message" role="status" class="text-sm" :class="ok ? 'text-success' : 'text-error'">{{ message }}</span>
  </div>
</template>
