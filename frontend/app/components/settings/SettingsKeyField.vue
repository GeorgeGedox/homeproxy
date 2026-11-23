<script setup lang="ts">
import type { components } from "~~/api/schema";

defineProps<{
  section: keyof components["schemas"]["KeysSet"];
  name: string;
  label: string;
  /** What stops working when the key is cleared. Set only while a feature needs this key. */
  needed?: string;
}>();
const { put, isSet } = useSettingsKeys();
</script>

<template>
  <SettingsSecretField
    :label="label" noun="key" :testid="`key-${name}`" :is-set="isSet(section, name) ?? false" :needed="needed"
    :save="(value: string) => put(section, name, value)"
  />
</template>
