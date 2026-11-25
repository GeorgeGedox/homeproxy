<script setup lang="ts">
const draft = defineModel<RouteDraft>({ required: true });
const show = (field: Parameters<typeof hasField>[1]) => hasField(draft.value.type, field);
const codes = [
  { label: "301 · Moved for good", value: 301 },
  { label: "302 · Moved for now", value: 302 },
  { label: "307 · Moved for now, keeps the method", value: 307 },
  { label: "308 · Moved for good, keeps the method", value: 308 },
] as const;
</script>

<template>
  <section class="space-y-4 border-t border-default pt-6">
    <h2 class="text-base font-medium text-highlighted">To</h2>
    <template v-if="show('redirect')">
      <UFormField label="URL" name="redirect.to">
        <UInput v-model="draft.redirect_to" placeholder="https://example.com" class="w-full" />
      </UFormField>
      <UFormField label="Kind" name="redirect.code">
        <USelect v-model="draft.redirect_code" :items="[...codes]" class="w-full" />
      </UFormField>
      <USwitch v-model="draft.keep_path" label="Keep the path and query" />
    </template>
    <template v-else>
      <UFormField label="Upstreams" name="upstreams" description="host:port or a URL, press Enter after each">
        <UInputTags v-model="draft.upstreams" add-on-blur :placeholder="draft.upstreams.length ? undefined : '192.168.1.10:8080'" class="w-full" />
      </UFormField>
      <!-- One upstream has nothing to choose between, and the route then saves it as off. -->
      <USwitch
        v-if="draft.upstreams.length > 1"
        v-model="draft.sticky"
        label="Sticky upstream"
        description="Send each client to the same upstream while it is up. Use it for apps that keep a session on one node, such as the Proxmox console."
      />
      <USwitch v-if="show('tls_upstream')" v-model="draft.tls_enabled" label="Connect over HTTPS" />
      <UFormField v-if="show('health_check')" label="Health check path" name="health_check.path" description="Empty checks that the port answers">
        <UInput v-model="draft.health_path" placeholder="/health" class="w-full" />
      </UFormField>
    </template>
  </section>
</template>
