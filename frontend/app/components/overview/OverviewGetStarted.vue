<script setup lang="ts">
const STEPS = [
  { title: "Add a domain", text: "The zone your services live under, such as example.com. A certificate is optional.", to: "/domains", icon: "lucide:globe" },
  { title: "Add a route", text: "Send a host or a path to a service on your network, LAN-only or public.", to: "/routes", icon: "lucide:route" },
  { title: "Apply", text: "HAProxy checks the config and serves it. Every apply can be rolled back.", icon: "lucide:check" },
];
</script>

<template>
  <section aria-labelledby="start-title" class="rounded-lg border border-default p-6">
    <h2 id="start-title" class="text-lg text-highlighted">Put your first service behind HomeProxy</h2>
    <p class="mt-1 text-sm text-muted">HAProxy is running. Three steps get a service answering on its own name.</p>
    <ol class="mt-6 grid gap-px overflow-hidden rounded-md border border-default bg-(--ui-border) md:grid-cols-3">
      <li v-for="(step, i) in STEPS" :key="step.title" class="flex flex-col gap-2 bg-default p-4">
        <span class="font-mono text-xs text-dimmed">0{{ i + 1 }}</span>
        <span class="flex items-center gap-2 text-highlighted"><UIcon :name="step.icon" class="size-4 text-muted" />{{ step.title }}</span>
        <span class="text-sm text-muted">{{ step.text }}</span>
        <UButton v-if="step.to" :to="step.to" :label="step.title" :color="i === 0 ? 'primary' : 'neutral'" :variant="i === 0 ? 'solid' : 'outline'" size="sm" class="mt-2 self-start" />
      </li>
    </ol>
  </section>
</template>
