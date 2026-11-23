<script setup lang="ts">
import type { LedTone } from "~/utils/health";

const pending = usePending();
const detailsOpen = ref(false);
const discardOpen = ref(false);

const conflict = computed(() => Boolean(pending.review.value?.conflict));
const visible = computed(() => Boolean(pending.failure.value) || pending.count.value > 0 || pending.staged.value || pending.handEdit.value || conflict.value);
/** What Discard removes: basic auth users are saved at once, so only the staged files go. */
const discarded = computed(() => pending.summary.value.filter((change) => change.kind !== "basic_auth").map(changeText));
const discardCount = computed(() => discarded.value.length || (pending.review.value?.changes.length ?? 0));

type Headline = { tone: LedTone; title: string; detail: string };

/** A failed apply or a conflict outranks what is staged. */
function problem(): Headline | undefined {
  const failure = pending.failure.value;
  if (failure) return { tone: "down", title: `Apply failed at ${failure.stage}`, detail: failure.staged ? "The live config is unchanged and your changes are still staged" : "The live config is unchanged" };
  if (conflict.value) return { tone: "unknown", title: "Staged files were edited on disk", detail: "Discard the staged changes to go on" };
}

function waiting(): Headline {
  if (pending.count.value) {
    const texts = pending.summary.value.map(changeText);
    const more = texts.length > 2 ? ` and ${texts.length - 2} more` : "";
    return { tone: "staged", title: `${texts.length} ${texts.length === 1 ? "change" : "changes"} staged`, detail: texts.slice(0, 2).join(", ") + more };
  }
  if (pending.staged.value) return { tone: "staged", title: "Config changes staged", detail: pending.review.value?.changes.map((c) => c.path).join(", ") ?? "" };
  const broken = pending.review.value?.generated_error;
  if (broken) return { tone: "down", title: "The saved config does not render", detail: broken };
  return { tone: "staged", title: "Saved config differs from what runs", detail: "A hand edit waits for an apply" };
}

const headline = computed(() => problem() ?? waiting());

async function discard(): Promise<void> {
  discardOpen.value = false;
  detailsOpen.value = false;
  await pending.discard();
}

async function apply(): Promise<void> {
  if (await pending.apply()) detailsOpen.value = false;
}
</script>

<template>
  <div
    v-if="visible"
    class="pointer-events-none fixed inset-x-2 bottom-2 z-40 flex justify-center sm:inset-x-4 sm:bottom-4 lg:left-[calc(15rem+1rem)]"
  >
    <div
      data-testid="pending-bar" role="status"
      class="pointer-events-auto flex w-full max-w-3xl items-center gap-3 rounded-lg border border-accented bg-elevated py-2 pr-2 pl-4 shadow-lg shadow-black/40"
    >
      <StatusLed :tone="headline.tone" />
      <div class="min-w-0 flex-1">
        <p class="truncate text-sm font-medium text-highlighted">{{ headline.title }}</p>
        <p class="hidden truncate text-xs text-muted sm:block">{{ headline.detail }}</p>
      </div>
      <UButton label="Details" color="neutral" variant="ghost" size="sm" @click="detailsOpen = true" />
      <UButton
        v-if="pending.staged.value" label="Discard" color="neutral" variant="ghost" size="sm" class="hidden sm:inline-flex"
        :disabled="pending.applying.value" @click="discardOpen = true"
      />
      <UButton
        :label="pending.failure.value ? 'Apply again' : 'Apply'" size="sm" :loading="pending.applying.value"
        :disabled="conflict" @click="apply"
      />
    </div>

    <ChangesDetails v-model:open="detailsOpen" @apply="apply" @discard="discardOpen = true" />

    <UModal
      v-model:open="discardOpen"
      :title="`Discard ${discardCount} staged ${discardCount === 1 ? 'change' : 'changes'}?`"
      description="They are removed for good. What HAProxy runs now stays as it is."
    >
      <template #body>
        <ul class="space-y-1 text-sm">
          <li v-for="text in discarded" :key="text">{{ text }}</li>
        </ul>
      </template>
      <template #footer>
        <UButton color="error" variant="soft" label="Discard changes" @click="discard" />
        <UButton color="neutral" variant="ghost" label="Keep them" @click="discardOpen = false" />
      </template>
    </UModal>
  </div>
</template>
