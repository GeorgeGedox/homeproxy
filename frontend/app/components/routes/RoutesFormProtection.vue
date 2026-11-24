<script setup lang="ts">
import type { RouteUser } from "~/utils/route";

const draft = defineModel<RouteDraft>({ required: true });
const users = defineModel<RouteUser[]>("users", { required: true });
const password = computed({ get: () => draft.value.auth === "basic", set: (on: boolean) => (draft.value.auth = on ? "basic" : "none") });
const replace = computed({ get: () => draft.value.geoip_mode === "replace", set: (on: boolean) => (draft.value.geoip_mode = on ? "replace" : "add") });
const blockNote = computed(() =>
  draft.value.exposure === "public" ? "Visitors from these countries get 403" : "Checked only while the site is public");
const listLabel = computed(() => (replace.value ? "Countries (replaces the global list)" : "Countries (adds to the global list)"));
</script>

<template>
  <section class="space-y-4 border-t border-default pt-6">
    <h2 class="text-base font-medium text-highlighted">Protection</h2>
    <template v-if="draft.type === 'http'">
      <USwitch v-model="password" label="Ask for a password" description="HomeProxy checks it before the service sees the request" />
      <UFormField v-if="password" label="Users" name="users" description="Passwords are write-only. Users take effect at the next apply.">
        <RoutesUsersField v-model="users" />
      </UFormField>
    </template>
    <USwitch v-model="draft.geoip_enabled" label="Block countries" :description="blockNote" />
    <USwitch
      v-if="draft.geoip_enabled" v-model="replace" label="Replace the global list"
      description="Off: these countries add to the global list. On: only these are blocked, and an empty list opens the site to every country"
    />
    <UFormField v-if="draft.geoip_enabled" :label="listLabel" name="geoip.block_countries">
      <GeoipCountryPicker v-model="draft.block_countries" :replaces="replace" />
    </UFormField>
    <p v-else-if="draft.block_countries.length" class="text-sm text-muted">
      Off. Kept for later: {{ draft.block_countries.map(countryName).join(", ") }}
    </p>
  </section>
</template>
