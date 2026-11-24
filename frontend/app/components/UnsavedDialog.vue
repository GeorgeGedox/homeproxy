<script setup lang="ts">
defineProps<{
  /** The question, such as "Discard your edits?". */
  title: string;
  /** What is not saved yet. */
  description?: string;
}>();
/** Whether the dialog shows: `useUnsavedGuard`'s `open`. */
const open = defineModel<boolean>("open", { required: true });
defineEmits<{ discard: [] }>();
</script>

<template>
  <UModal v-model:open="open" :title :description>
    <template #footer>
      <UButton color="error" variant="soft" label="Discard edits" @click="$emit('discard')" />
      <UButton color="neutral" variant="ghost" label="Keep editing" @click="open = false" />
    </template>
  </UModal>
</template>
