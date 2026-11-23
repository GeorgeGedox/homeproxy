<script setup lang="ts">
import type { components } from "~~/api/schema";

const open = defineModel<boolean>("open", { required: true });
const emit = defineEmits<{ apply: []; discard: [] }>();

const api = useApi();
const pending = usePending();
const cloudflare = ref<components["schemas"]["ZoneResult"][]>([]);

const review = computed(() => pending.review.value);
const diffs = computed(() => [
  { label: "Configuration files", value: "config", diff: review.value?.config_diff ?? "" },
  { label: "Generated HAProxy files", value: "generated", diff: review.value?.generated_diff ?? "" },
].filter((item) => item.diff));

/** What the next reconcile would change in Cloudflare, read only while the details are open. */
watch(open, async (isOpen) => {
  if (!isOpen) return;
  await pending.refresh();
  // The records list no zones while the integration is off.
  cloudflare.value = (await api.GET("/api/v1/cloudflare/records")).data?.zones ?? [];
});
</script>

<template>
  <USlideover v-model:open="open" title="Staged changes" description="What the next apply changes, and the files behind it" :ui="{ content: 'max-w-2xl' }">
    <template #body>
      <div class="space-y-6">
        <UAlert
          v-if="pending.failure.value" color="error" variant="subtle" icon="lucide:circle-alert"
          :title="`Apply failed at ${pending.failure.value.stage}`"
        >
          <template #description>
            <pre class="font-mono text-xs whitespace-pre-wrap">{{ pending.failure.value.errors.join("\n") }}</pre>
          </template>
        </UAlert>
        <UAlert
          v-if="review?.conflict" color="warning" variant="subtle" icon="lucide:triangle-alert"
          title="These files were edited on disk after you staged them" :description="review.conflicting_paths.join(', ')"
        />
        <UAlert
          v-if="review?.generated_error" color="error" variant="subtle" :description="review.generated_error"
          :title="pending.staged.value ? 'The staged config does not render' : 'The saved config does not render'"
        />

        <ul v-if="pending.summary.value.length" class="divide-y divide-default rounded-md border border-default">
          <li v-for="change in pending.summary.value" :key="changeText(change)" class="flex items-center gap-3 px-3 py-2 text-sm">
            <UIcon :name="CHANGE_ICONS[change.action]" class="size-4 text-dimmed" />
            {{ changeText(change) }}
          </li>
        </ul>
        <p v-else-if="pending.handEdit.value" class="text-sm text-muted">
          Nothing is staged, but the saved files differ from what HAProxy runs. Apply loads them.
        </p>

        <ChangesCloudflare v-if="cloudflare.some((zone) => zone.changes?.length || zone.conflicts?.length || zone.errors?.length)" :zones="cloudflare" />

        <UAccordion v-if="diffs.length" type="multiple" :items="diffs">
          <template #body="{ item }">
            <ChangesDiffView :diff="item.diff" />
          </template>
        </UAccordion>
      </div>
    </template>
    <template #footer>
      <div class="flex w-full justify-end gap-2">
        <UButton v-if="pending.staged.value" label="Discard" color="neutral" variant="ghost" :disabled="pending.applying.value" @click="emit('discard')" />
        <UButton label="Apply" :loading="pending.applying.value" :disabled="review?.conflict" @click="emit('apply')" />
      </div>
    </template>
  </USlideover>
</template>
