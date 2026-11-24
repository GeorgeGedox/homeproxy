<script setup lang="ts">
/** `label` names the direction in every control, such as "request header". */
defineProps<{ label: string }>();
const headers = defineModel<{ name: string; value: string }[]>({ required: true });
</script>

<template>
  <div class="space-y-2">
    <div v-for="(header, index) in headers" :key="index" class="flex gap-2">
      <UInput v-model="header.name" placeholder="Header-Name" :aria-label="`${label} name`" class="flex-1" />
      <UInput v-model="header.value" placeholder="value" :aria-label="`${label} value`" class="flex-1" />
      <UButton icon="lucide:x" color="neutral" variant="ghost" :aria-label="`Remove ${label}`" @click="headers.splice(index, 1)" />
    </div>
    <UButton icon="lucide:plus" color="neutral" variant="subtle" :label="`Add ${label}`" @click="headers.push({ name: '', value: '' })" />
  </div>
</template>
