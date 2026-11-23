<script setup lang="ts">
const props = defineProps<{ failure?: unknown }>();
const draft = defineModel<RouteDraft>({ required: true });

/** Whether a field here differs from its default, so the section starts open and nothing set stays out of sight. */
const changed = (d: RouteDraft) =>
  [d.strip_prefix, d.websocket, d.tls_enabled && !d.tls_verify, d.request_headers.length + d.response_headers.length > 0].some(Boolean);
const open = ref(changed(draft.value));
// A refused field in this section must be seen, so it opens; a field elsewhere leaves it as it is.
const OWN_FIELDS = ["strip_prefix", "websocket", "tls_upstream", "request_headers", "response_headers"];
watch(() => props.failure, (failure) => {
  const field = failure ? apiFieldError(failure, "").field : undefined;
  if (field && OWN_FIELDS.includes(field.split(".")[0] ?? "")) open.value = true;
});
</script>

<template>
  <UCollapsible v-model:open="open" :unmount-on-hide="false" class="border-t border-default pt-4">
    <UButton
      label="Advanced" color="neutral" variant="ghost" trailing-icon="lucide:chevron-down" class="group -mx-2.5 font-medium"
      :ui="{ trailingIcon: 'transition-transform group-data-[state=open]:rotate-180' }"
    />
    <template #content>
      <div class="space-y-4 pt-4">
        <USwitch
          v-if="draft.path_prefix && draft.path_prefix !== '/'" v-model="draft.strip_prefix"
          :label="`Remove ${draft.path_prefix} before forwarding`"
        />
        <USwitch v-model="draft.websocket" label="WebSocket" />
        <USwitch v-if="draft.tls_enabled" v-model="draft.tls_verify" label="Verify the upstream certificate" />
        <UFormField label="Request headers" name="request_headers" hint="Sent to the upstream">
          <RoutesHeadersField v-model="draft.request_headers" label="request header" />
        </UFormField>
        <UFormField label="Response headers" name="response_headers" hint="Sent to the client">
          <RoutesHeadersField v-model="draft.response_headers" label="response header" />
        </UFormField>
      </div>
    </template>
  </UCollapsible>
</template>
