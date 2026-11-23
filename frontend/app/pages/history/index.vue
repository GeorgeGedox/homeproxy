<script setup lang="ts">
import type { SnapshotInfo } from "~/utils/changes";

const history = useHistory();
const pending = usePending();
const asking = ref<SnapshotInfo>();
const refused = ref("");
// Applies come with snapshots to restore; Activity is the audit log of changes saved at once.
const tab = ref<"applies" | "activity">("applies");
const tabs = [{ label: "Applies", value: "applies" }, { label: "Activity", value: "activity" }];

function ask(snapshot: SnapshotInfo): void {
  refused.value = "";
  asking.value = snapshot;
}
async function restore(): Promise<void> {
  if (!asking.value) return;
  refused.value = (await history.restore(asking.value.id)) ?? "";
  if (!refused.value) asking.value = undefined;
}
</script>

<template>
  <UDashboardPanel id="history">
    <template #header>
      <UDashboardNavbar title="History" />
      <UDashboardToolbar>
        <UTabs v-model="tab" :items="tabs" :content="false" variant="link" color="neutral" size="sm" aria-label="History" class="-mb-px" />
      </UDashboardToolbar>
    </template>
    <template #body>
      <HistoryActivity v-if="tab === 'activity'" />
      <template v-else>
        <section v-if="history.snapshots.value?.length" class="rounded-lg border border-default" aria-label="Applies">
          <ul class="divide-y divide-default">
            <li v-for="(snapshot, index) in history.snapshots.value" :key="snapshot.id" class="px-4 py-3" :aria-label="dateTime(snapshot.created)">
              <div class="flex flex-wrap items-center gap-x-3 gap-y-2">
                <!-- Only the live snapshot gets a light; the others keep its column. -->
                <StatusLed tone="up" :class="index > 0 && 'invisible'" />
                <div class="min-w-0 flex-1 basis-64">
                  <p class="text-sm text-highlighted">{{ snapshotText(snapshot) }}</p>
                  <p class="text-xs text-muted">
                    {{ timeAgo(snapshot.created) }} · {{ dateTime(snapshot.created) }}
                    <template v-if="index === 0"> · <span class="text-success">Live now</span></template>
                  </p>
                </div>
                <!-- On a phone the actions take their own line, their labels under the text above (ghost buttons pad 10px). -->
                <div v-if="index > 0" class="flex gap-1 max-sm:basis-full max-sm:pl-2.5">
                  <UButton
                    :label="history.opened.value === snapshot.id ? 'Hide changes' : 'Show changes'" color="neutral" variant="ghost" size="sm"
                    :aria-expanded="history.opened.value === snapshot.id" @click="history.toggle(snapshot.id)"
                  />
                  <UButton label="Restore" icon="lucide:undo-2" color="neutral" variant="ghost" size="sm" @click="ask(snapshot)" />
                </div>
              </div>
              <div v-if="history.opened.value === snapshot.id" class="mt-3 pl-5">
                <p class="mb-2 text-xs text-muted">What restoring it would change, from the files as they are now</p>
                <p v-if="history.diffs.value[snapshot.id] === null" class="text-sm text-error">The changes did not load. Hide them and show them again to retry.</p>
                <ChangesDiffView v-else-if="snapshot.id in history.diffs.value" :diff="history.diffs.value[snapshot.id] ?? ''" />
                <USkeleton v-else class="h-16 w-full" />
              </div>
            </li>
          </ul>
        </section>
        <UAlert v-else-if="history.snapshots.value === null" color="error" variant="subtle" title="The applies could not be loaded" />
        <UEmpty
          v-else-if="history.snapshots.value" icon="lucide:history" title="No applies yet"
          description="Each apply keeps a snapshot here, up to 50, so you can see what it changed and go back to it."
        />

        <section v-if="!pending.staged.value" class="flex flex-col gap-3 rounded-lg border border-default px-4 py-3 sm:flex-row sm:items-center" aria-label="Apply saved files">
          <p class="min-w-0 flex-1 text-xs text-muted">
            HAProxy runs something other than the saved files, for example after a hand edit or a failed rollback? Apply them again.
          </p>
          <UButton label="Apply saved files" color="neutral" variant="outline" size="sm" :loading="pending.applying.value" class="self-start sm:self-auto" @click="pending.apply()" />
        </section>
      </template>

      <UModal :open="Boolean(asking)" :title="`Restore the config from ${asking ? dateTime(asking.created) : ''}?`" @update:open="(open) => !open && (asking = undefined)">
        <template #body>
          <ul class="list-disc space-y-1 pl-5 text-sm text-muted">
            <li>Its files are staged, not live. Apply in the bar at the bottom makes them live.</li>
            <li>Your password, dashboard address and session length stay as they are.</li>
            <li v-if="pending.staged.value" class="text-highlighted">
              It replaces what is staged now{{ pending.count.value ? `: ${pending.count.value === 1 ? "1 change" : `${pending.count.value} changes`}` : "" }}.
            </li>
          </ul>
          <p v-if="refused" class="mt-3 text-sm text-error">{{ refused }}</p>
        </template>
        <template #footer>
          <UButton label="Stage restore" @click="restore" />
          <UButton color="neutral" variant="ghost" label="Cancel" @click="asking = undefined" />
        </template>
      </UModal>
    </template>
  </UDashboardPanel>
</template>
