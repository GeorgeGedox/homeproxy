<script setup lang="ts">
import type { components } from "~~/api/schema";

const geoip = defineModel<components["schemas"]["Geoip"]>({ required: true });
const { isSet } = useSettingsKeys();
</script>

<template>
  <SettingsSection id="geoip" title="GeoIP" description="Where country data comes from. Apply a provider change before refreshing the data under Security, Countries.">
    <UFormField label="GeoIP provider" help="dbip-lite needs nothing. maxmind needs a free MaxMind license key."><USelect v-model="geoip.provider" :items="['dbip-lite', 'maxmind']" class="w-full" /></UFormField>
    <SettingsKeyField
      v-if="geoip.provider === 'maxmind' || isSet('geoip', 'maxmind_license_key')"
      section="geoip"
      name="maxmind_license_key"
      label="MaxMind license key"
      needed="GeoIP data can no longer be downloaded from MaxMind, so the GeoIP page cannot refresh and country blocks stop updating."
    />
  </SettingsSection>
</template>
