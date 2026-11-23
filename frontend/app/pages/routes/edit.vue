<script setup lang="ts">
const query = useRoute().query;
const text = (key: string) => (typeof query[key] === "string" ? query[key] : undefined);
const host = text("host");
const editor = useRouteEditor({ name: text("name"), copy: text("copy"), host, path: text("path") });
const guard = useUnsavedGuard(editor.dirty);

const label = computed(() => (editor.original.value ? routeTarget(editor.original.value) : ""));
const title = computed(() => {
  if (!editor.isNew) return label.value ? `Edit ${label.value}` : "Edit";
  return host ? `Add path to ${host}` : "Add site";
});

async function submit(): Promise<void> {
  if (await editor.save()) await navigateTo("/routes");
}
</script>

<template>
  <UDashboardPanel id="route-edit">
    <template #header>
      <UDashboardNavbar :title="title">
        <template #leading>
          <UButton to="/routes" icon="lucide:arrow-left" color="neutral" variant="ghost" size="sm" aria-label="Back to Routes" />
        </template>
      </UDashboardNavbar>
    </template>
    <template #body>
      <div v-if="editor.loadError.value" class="max-w-2xl space-y-4">
        <UAlert color="error" variant="subtle" :description="editor.loadError.value" />
        <UButton to="/routes" color="neutral" variant="subtle" label="Back to Routes" />
      </div>
      <RoutesForm
        v-else-if="!editor.loading.value"
        v-model:draft="editor.draft.value" v-model:users="editor.users.value"
        :original="editor.original.value" :adding-path="Boolean(host) && editor.isNew" :site-others="editor.siteOthers.value"
        :site-changed="editor.siteChanged.value" :lan-ip="editor.lanIp.value" :cloudflare-available="editor.cloudflareAvailable.value"
        :saving="editor.saving.value" :error="editor.saveError.value" :failure="editor.failure.value"
        :problems="editor.problems.value" @submit="submit"
      />
      <UModal
        v-model:open="guard.open.value" :title="label ? `Discard your edits to ${label}?` : 'Discard your edits?'"
      >
        <template #footer>
          <UButton color="error" variant="soft" label="Discard edits" @click="guard.discard" />
          <UButton color="neutral" variant="ghost" label="Keep editing" @click="guard.open.value = false" />
        </template>
      </UModal>
    </template>
  </UDashboardPanel>
</template>
