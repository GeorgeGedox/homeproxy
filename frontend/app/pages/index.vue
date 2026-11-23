<script setup lang="ts">
const { status, data, fresh, health, attention, traffic, requests, classes, countries, bans } = useOverview();
</script>

<template>
  <UDashboardPanel id="overview">
    <template #header>
      <UDashboardNavbar title="Overview" />
    </template>
    <template #body>
      <OverviewStatus v-if="status" :status="status" :bans="bans" />
      <UAlert v-else-if="status === null" color="error" variant="subtle" title="The status could not be loaded" description="The dashboard cannot reach its API. HAProxy keeps serving the last applied config." />

      <OverviewAttention v-if="attention.length" :items="attention" />

      <OverviewGetStarted v-if="fresh" />
      <template v-else-if="data">
        <div class="grid grid-cols-1 gap-4 lg:grid-cols-3 sm:gap-6">
          <OverviewTraffic class="lg:col-span-2" :buckets="traffic" :requests="requests" :errors="classes['5xx']" />
          <OverviewRoutes :rows="health" />
        </div>
        <div class="grid grid-cols-1 gap-4 md:grid-cols-3 sm:gap-6">
          <OverviewStatusCodes :classes="classes" />
          <OverviewCountries :countries="countries" />
          <OverviewCertificates :certificates="data.certificates" />
        </div>
      </template>
    </template>
  </UDashboardPanel>
</template>
