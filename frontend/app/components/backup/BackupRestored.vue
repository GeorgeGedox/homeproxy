<script setup lang="ts">
const reload = () => window.location.reload();
</script>

<template>
  <div class="space-y-4">
    <p class="text-sm text-muted">The dashboard restarts now with the certificate from the backup.</p>
    <ol class="list-decimal space-y-2 pl-5 text-sm text-highlighted">
      <li>Wait a few seconds, then reload this page.</li>
      <li>If your browser warns about the certificate again, accept it.</li>
      <li>Log in with the password from the backup.</li>
    </ol>
    <UButton block icon="lucide:refresh-cw" label="Reload" @click="reload" />
  </div>
</template>
