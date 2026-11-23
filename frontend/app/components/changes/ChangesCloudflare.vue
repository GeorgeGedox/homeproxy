<script setup lang="ts">
import type { components } from "~~/api/schema";

type Zone = components["schemas"]["ZoneResult"];

defineProps<{ zones: Zone[] }>();
</script>

<template>
  <div class="space-y-2">
    <h2 class="text-lg font-semibold text-highlighted">Cloudflare records</h2>
    <template v-for="zone in zones" :key="zone.zone">
      <ul class="text-sm">
        <li v-for="change in zone.changes" :key="change.name">{{ change.action }}: {{ change.name }}<template v-if="change.action !== 'delete'"> to {{ change.content }}</template></li>
      </ul>
      <UAlert
        v-for="conflict in zone.conflicts" :key="conflict.name" color="warning" variant="subtle" icon="lucide:triangle-alert"
        :title="`${conflict.name} is a hand-made record, so route ${conflict.route} will not manage it`"
      />
      <UAlert v-for="error in zone.errors" :key="error" color="error" variant="subtle" :description="error" />
    </template>
  </div>
</template>
