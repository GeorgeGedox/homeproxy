<template>
  <SettingsSection title="Download backup" description="Config, secrets, certificates, history and the audit log, as one tar.gz archive.">
    <p class="flex items-start gap-2 text-sm text-muted">
      <UIcon name="lucide:triangle-alert" class="mt-0.5 size-4 shrink-0 text-warning" />
      The archive holds secrets and certificate keys, and it is not encrypted. Keep it private.
    </p>
    <UButton to="/api/v1/backup" external download icon="lucide:download" label="Download backup" />
  </SettingsSection>
</template>
