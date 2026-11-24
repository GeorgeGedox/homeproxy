<script setup lang="ts">
import type { RouteUser } from "~/utils/route";

const users = defineModel<RouteUser[]>({ required: true });
</script>

<template>
  <div class="space-y-2">
    <div v-for="(user, index) in users" :key="index" class="flex gap-2">
      <UInput v-model="user.name" :readonly="user.existing" placeholder="User name" aria-label="User name" class="flex-1" />
      <UInput
        v-model="user.password" type="password" autocomplete="new-password" class="flex-1"
        :placeholder="user.existing ? 'Unchanged' : 'Password'" aria-label="User password"
      />
      <UButton icon="lucide:x" color="neutral" variant="ghost" aria-label="Remove user" @click="users.splice(index, 1)" />
    </div>
    <UButton icon="lucide:plus" color="neutral" variant="subtle" label="Add user" @click="users.push({ name: '', password: '', existing: false })" />
  </div>
</template>
