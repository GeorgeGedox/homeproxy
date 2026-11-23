<script setup lang="ts">
defineProps<{ addingPath: boolean }>();
const draft = defineModel<RouteDraft>({ required: true });
const show = (field: Parameters<typeof hasField>[1]) => hasField(draft.value.type, field);
</script>

<template>
  <section class="space-y-4">
    <h2 class="text-base font-medium text-highlighted">Where</h2>
    <URadioGroup
      v-if="!addingPath" v-model="draft.type" :items="ROUTE_TYPES" variant="card" color="neutral" legend="Type"
      :ui="{ fieldset: 'grid grid-cols-1 gap-2 sm:grid-cols-2', legend: 'sr-only' }"
    />
    <UFormField v-if="show('hosts')" label="Hosts" name="hosts" hint="Press Enter after each">
      <UInputTags v-model="draft.hosts" add-on-blur :placeholder="draft.hosts.length ? undefined : 'app.example.com'" class="w-full" />
    </UFormField>
    <UFormField v-if="show('listen_port')" label="Port" name="listen_port" hint="HomeProxy listens on it for this service">
      <UInputNumber v-model="draft.listen_port" :min="1" :max="65535" :format-options="{ useGrouping: false }" class="w-full" />
    </UFormField>
    <UFormField v-if="show('path')" label="Path" name="path_prefix" hint="/ is the whole site">
      <UInput v-model="draft.path_prefix" placeholder="/" class="w-full" />
    </UFormField>
  </section>
</template>
