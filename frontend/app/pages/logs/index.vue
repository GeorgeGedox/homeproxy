<script setup lang="ts">
const api = useApi();
// Short tab names, so all five fit a phone; each tab's header says what the log is and its size.
const NAMES = { access: "Requests", haproxy: "HAProxy", api: "Dashboard", dns: "DNS", spoa: "Bouncer" } as const;
const TITLES = { access: "Requests", haproxy: "HAProxy messages", api: "Dashboard output", dns: "DNS server output", spoa: "CrowdSec bouncer output" } as const;
const tab = ref<keyof typeof NAMES>("access");
const { data: files } = useAsyncData("log-files", async () => (await api.GET("/api/v1/logs/files")).data ?? null, { watch: [tab] });
const tabs = computed(() => (files.value ?? []).map((log) => ({ label: NAMES[log.source], value: log.source })));
const stored = computed(() => files.value?.find((log) => log.source === tab.value));
</script>

<template>
  <UDashboardPanel id="logs">
    <template #header>
      <UDashboardNavbar title="Logs" />
      <UDashboardToolbar>
        <UTabs v-model="tab" :items="tabs" :content="false" variant="link" color="neutral" size="sm" aria-label="Log" class="-mb-px" />
      </UDashboardToolbar>
    </template>
    <template #body>
      <p v-if="files === null" class="text-sm text-error">The list of logs could not be loaded.</p>
      <LogsTail v-if="tab === 'access'" :size="stored?.size" />
      <LogsStored v-else-if="stored" :key="stored.source" :log="stored" :name="TITLES[stored.source]" />
    </template>
  </UDashboardPanel>
</template>
