<script setup lang="ts">
/** What an archive holds and leaves out, as `backup/layout.py` lists it. */
const contents = [
  { label: "Holds", value: "Config and routes, secrets, certificates, the ACME account, the dashboard certificate, history and the audit log" },
  { label: "Leaves out", value: "Staged changes, metrics, stored logs and the generated HAProxy files" },
];
</script>

<template>
  <SettingsSection title="Download backup" description="Everything a new machine needs to run this HomeProxy again, in one .tar.gz file.">
    <dl class="divide-y divide-default rounded-md border border-default text-sm">
      <div v-for="item in contents" :key="item.label" class="flex flex-col gap-0.5 px-3 py-2 sm:flex-row sm:gap-4">
        <dt class="shrink-0 text-muted sm:w-24">{{ item.label }}</dt>
        <dd class="text-highlighted">{{ item.value }}</dd>
      </div>
    </dl>
    <div class="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
      <p class="flex items-start gap-2 text-sm text-muted">
        <UIcon name="lucide:triangle-alert" class="mt-0.5 size-4 shrink-0 text-warning" />
        Not encrypted. Keep it private.
      </p>
      <UButton to="/api/v1/backup" external download icon="lucide:download" label="Download backup" class="self-start sm:self-auto" />
    </div>
  </SettingsSection>
</template>
