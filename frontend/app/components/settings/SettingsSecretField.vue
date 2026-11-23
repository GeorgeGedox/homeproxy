<script setup lang="ts">
/** A write-only secret: shown only as set or not, replaced or cleared, never read back. */
const props = defineProps<{
  label: string;
  isSet: boolean;
  /** "key" or "URL", for the button labels. */
  noun: string;
  testid: string;
  /** Saves the value, or clears it with "". Returns an error message, or "". */
  save: (value: string) => Promise<string>;
  /** What stops working when it is cleared. Set only while a feature needs it, so Clear asks first. */
  needed?: string;
}>();
const editing = ref(false);
const confirming = ref(false);
const value = ref("");
const error = ref("");
const busy = ref(false);

async function write(next: string): Promise<void> {
  busy.value = true;
  error.value = await props.save(next);
  busy.value = false;
  if (error.value) return;
  editing.value = confirming.value = false;
  value.value = "";
}

const clear = () => (props.needed ? (confirming.value = true) : write(""));
</script>

<template>
  <UFormField :label="label" :error="error || undefined">
    <div class="flex flex-wrap items-center gap-2">
      <span class="inline-flex items-center gap-2 text-sm" :class="isSet ? 'text-highlighted' : 'text-muted'">
        <StatusLed :tone="isSet ? 'up' : 'off'" /><span :data-testid="testid">{{ isSet ? "Set" : "Not set" }}</span>
      </span>
      <template v-if="editing">
        <UInput v-model="value" type="password" autocomplete="off" :aria-label="`New ${label}`" class="min-w-0 flex-1" />
        <UButton :loading="busy" :disabled="!value" :label="`Save ${noun}`" :aria-label="`Save ${label}`" @click="write(value)" />
        <UButton color="neutral" variant="ghost" label="Cancel" @click="editing = false; value = ''" />
      </template>
      <template v-else>
        <UButton size="sm" color="neutral" variant="outline" :label="isSet ? 'Replace' : 'Add'" :aria-label="`${isSet ? 'Replace' : 'Add'} ${label}`" @click="editing = true" />
        <UButton v-if="isSet" size="sm" color="error" variant="ghost" label="Clear" :aria-label="`Clear ${label}`" :loading="busy && !confirming" @click="clear" />
      </template>
    </div>
    <UModal v-model:open="confirming" :title="`Clear ${label}?`" :description="needed">
      <template #footer>
        <UButton color="neutral" variant="outline" :label="`Keep the ${noun}`" @click="confirming = false" />
        <UButton color="error" :loading="busy" :label="`Clear the ${noun}`" @click="write('')" />
      </template>
    </UModal>
  </UFormField>
</template>
