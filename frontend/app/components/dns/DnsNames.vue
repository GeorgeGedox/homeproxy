<script setup lang="ts">
const props = defineProps<{ names: DnsName[] }>();
defineEmits<{ remove: [string] }>();
const words = { live: "", staged: "staged", removing: "removed at the next apply" } as const;
const extraCount = computed(() => props.names.filter((name) => !name.route && name.state !== "removing").length);
</script>

<template>
  <section class="rounded-lg border border-default" aria-label="Names it answers">
    <header class="flex items-center gap-3 rounded-t-lg border-b border-default bg-muted px-4 py-3">
      <div class="min-w-0 flex-1">
        <h2 class="text-sm text-highlighted">Names it answers</h2>
        <p class="text-xs text-muted">Your sites, added for you, and {{ extraCount === 1 ? "1 extra record" : `${extraCount || "no"} extra records` }}</p>
      </div>
    </header>
    <ul v-if="names.length" class="divide-y divide-default">
      <li v-for="row in names" :key="row.name" :class="row.state === 'removing' && 'opacity-60'">
        <!-- A name from a site opens the site's editor; the whole row is the link. -->
        <ULink
          raw as="div" :to="row.route ? { path: '/routes/edit', query: { name: row.route } } : undefined"
          class="flex flex-wrap items-center gap-x-3 gap-y-1 px-4 py-2 text-sm" :class="row.route && 'group transition-colors [--focus-offset:-2px] hover:bg-elevated'"
        >
          <span class="min-w-0 truncate font-mono text-highlighted">{{ row.name }}</span>
          <span class="font-mono text-muted"><span class="text-dimmed">→ </span>{{ row.target }}</span>
          <span v-if="row.was" class="font-mono text-xs text-dimmed">was {{ row.was }}</span>
          <span v-if="words[row.state]" class="text-xs text-primary">{{ words[row.state] }}</span>
          <span class="flex-1" />
          <template v-if="!row.route && row.state !== 'removing'">
            <span class="text-xs text-muted">extra record</span>
            <UButton icon="lucide:x" color="neutral" variant="ghost" size="xs" :aria-label="`Remove ${row.name}`" @click="$emit('remove', row.name)" />
          </template>
          <UIcon v-if="row.route" name="lucide:chevron-right" class="size-4 shrink-0 text-dimmed group-hover:text-highlighted" />
        </ULink>
      </li>
    </ul>
    <p v-else class="px-4 py-3 text-sm text-muted">No names yet. Each site's host is added here, and you can add a device such as a NAS.</p>
  </section>
</template>
