<script setup lang="ts">
import type { DropdownMenuItem } from "@nuxt/ui";
import type { LedTone } from "~/utils/health";
import type { Decision } from "~/composables/useDecisions";

const PAGE_SIZE = 25;
const props = defineProps<{ crowdsec: ReturnType<typeof useDecisions> }>();
const emit = defineEmits<{ allowlisted: [] }>();
const list = props.crowdsec;
const { state: bouncer, known } = useBouncerState();
const { configStaged } = usePending();
const search = ref("");
const page = ref(1);
/** The blocklist decision whose unban asks first. */
const asking = ref<Decision>();

/** The bouncer decides the light: it blocks whether or not the dashboard can list decisions. Null means CrowdSec is off. */
const light = computed((): { tone: LedTone; text: string } => {
  if (!known.value) return { tone: "off", text: "Checking…" };
  if (bouncer.value === "down") return { tone: "down", text: "Bouncer down" };
  if (bouncer.value === "up") return { tone: "up", text: "Blocking" };
  if (bouncer.value === "starting") return { tone: "unknown", text: "Bouncer starting" };
  return { tone: "off", text: "Off" };
});
const crowdsecOff = computed(() => known.value && bouncer.value === null);
// Blocklists can hold thousands of decisions. They are hidden until asked for or searched, and the list is paged here.
const showLists = ref(false);
const fromLists = computed(() => list.decisions.value.filter((item) => FROM_BLOCKLIST.includes(item.origin)));
const listed = computed(() => fromLists.value.length);
/** The newest pull that brought blocklist decisions. A pull with nothing new leaves no trace in the LAPI. */
const listsUpdated = computed(() => fromLists.value.map((item) => item.created_at).sort().at(-1));
const listsText = computed(() => {
  const count = `Blocklists: ${formatCount(listed.value)} ${listed.value === 1 ? "decision" : "decisions"}`;
  return listsUpdated.value ? `${count}, updated ${timeAgo(listsUpdated.value)}` : count;
});
const matching = computed(() => {
  const term = search.value.trim();
  if (term) return list.decisions.value.filter((item) => item.value.includes(term));
  return showLists.value ? list.decisions.value : list.decisions.value.filter((item) => !FROM_BLOCKLIST.includes(item.origin));
});
const visible = computed(() => matching.value.slice((page.value - 1) * PAGE_SIZE, page.value * PAGE_SIZE));
watch([search, showLists], () => (page.value = 1));
// An unban can empty the last page; show the one before it.
const pages = computed(() => Math.max(1, Math.ceil(matching.value.length / PAGE_SIZE)));
watch(pages, (count) => (page.value = Math.min(page.value, count)));

const left = (item: Decision) => `${duration(Math.max(0, (new Date(item.expires_at).getTime() - Date.now()) / 1000))} left`;
// Most scenarios are CrowdSec's own, so their author prefix says nothing.
const scenario = (item: Decision) => item.scenario.replace(/^crowdsecurity\//, "");
const meta = (item: Decision) => [item.type, left(item), item.scenario ? `${originName(item.origin)}: ${scenario(item)}` : originName(item.origin)].join(" · ");

function unban(item: Decision): void {
  if (FROM_BLOCKLIST.includes(item.origin)) asking.value = item;
  else void list.unban(item);
}
async function allowlist(item: Decision, alsoUnban: boolean): Promise<void> {
  asking.value = undefined;
  if (await (alsoUnban ? list.allowlistAndUnban(item) : list.allowlist(item))) emit("allowlisted");
}
async function unbanOnly(item: Decision): Promise<void> {
  asking.value = undefined;
  await list.unban(item);
}

const menu = (item: Decision): DropdownMenuItem[] => [
  ...(list.longerThan(item).length
    ? [{ label: "Extend", children: list.longerThan(item).map(({ label, value }) => ({ label, onSelect: () => void list.extend(item, value) })) }]
    : []),
  ...(FROM_BLOCKLIST.includes(item.origin) ? [{ label: "Allowlist", disabled: configStaged.value, onSelect: () => void allowlist(item, false) }] : []),
];
</script>

<template>
  <section class="rounded-lg border border-default" aria-label="CrowdSec">
    <header class="flex flex-wrap items-center gap-3 rounded-t-lg border-b border-default bg-muted px-4 py-3">
      <UTooltip :text="light.text"><StatusLed :tone="light.tone" :label="light.text" /></UTooltip>
      <div class="min-w-0 flex-1">
        <h2 class="text-sm text-highlighted">CrowdSec <span class="text-muted">· {{ light.text }}</span></h2>
        <!-- With no decisions the body says "Nobody is banned right now", so "0 bans" here would repeat it. -->
        <p v-if="!list.unconfigured.value && list.loaded.value && list.decisions.value.length" class="text-xs text-muted">{{ list.summary.value }}</p>
      </div>
      <UInput
        v-if="list.decisions.value.length || search" v-model="search" icon="lucide:search" placeholder="Find an address" aria-label="Find an address"
        size="sm" class="w-full sm:w-56"
      />
    </header>
    <p v-if="list.error.value" class="border-b border-default px-4 py-2 text-sm text-error">{{ list.error.value }}</p>
    <p v-if="bouncer === 'down'" class="border-b border-default px-4 py-2 text-sm text-error">
      Requests are not checked while the bouncer is down: they pass through. The Logs page has its output.
    </p>
    <p v-if="crowdsecOff" class="px-4 py-3 text-sm text-muted">
      CrowdSec bans clients that attack your sites. Turn it on and set its API URL and machine login <ULink to="/settings#crowdsec" class="text-highlighted underline">in Settings</ULink>, then apply.
    </p>
    <p v-else-if="list.unconfigured.value" class="px-4 py-3 text-sm text-muted">
      The dashboard cannot list decisions without CrowdSec's API URL and machine login. Set them <ULink to="/settings#crowdsec" class="text-highlighted underline">in Settings</ULink>, then apply.
    </p>
    <ul v-else-if="visible.length" class="divide-y divide-default" aria-label="Decisions">
      <li v-for="item in visible" :key="item.id" class="flex flex-wrap items-center gap-x-3 gap-y-1 px-4 py-2 text-sm" :aria-label="item.value">
        <StatusLed :tone="item.type === 'ban' ? 'down' : 'unknown'" :label="item.type" />
        <span class="min-w-0 truncate font-mono text-highlighted">{{ item.value }}</span>
        <span class="order-last basis-full pl-5 text-xs text-muted sm:order-none sm:basis-auto sm:pl-0">{{ meta(item) }}</span>
        <span class="flex-1" />
        <UButton size="xs" color="neutral" variant="outline" label="Unban" :aria-label="`Unban ${item.value}`" @click="unban(item)" />
        <UDropdownMenu v-if="menu(item).length" :items="menu(item)" :content="{ align: 'end' }">
          <UButton icon="lucide:ellipsis" color="neutral" variant="ghost" size="xs" :aria-label="`More for ${item.value}`" />
        </UDropdownMenu>
      </li>
    </ul>
    <p v-else-if="list.loaded.value && !list.error.value" class="px-4 py-3 text-sm text-muted">
      {{ search ? "No decision matches this address." : listed ? "Nothing detected here or added by hand." : "Nobody is banned right now." }}
    </p>
    <div v-if="list.loaded.value && !list.unconfigured.value && !search" class="flex flex-wrap items-center gap-x-3 border-t border-default px-4 py-2 text-sm">
      <span class="text-muted">{{ listsText }}</span>
      <UButton v-if="listed" :label="showLists ? 'Hide them' : 'Show them'" color="neutral" variant="link" size="sm" class="px-0" @click="showLists = !showLists" />
      <UButton v-else to="https://app.crowdsec.net" external target="_blank" label="Enroll the engine in the CrowdSec console" color="neutral" variant="link" size="sm" class="px-0" />
    </div>
    <div v-if="matching.length > PAGE_SIZE" class="border-t border-default px-4 py-2">
      <UPagination v-model:page="page" :total="matching.length" :items-per-page="PAGE_SIZE" size="sm" />
    </div>

    <UModal :open="Boolean(asking)" :title="`${asking?.value} is on a blocklist`" @update:open="(open) => !open && (asking = undefined)">
      <template #description>
        Unban lets it in until the next blocklist pull, when it may come back. Allowlist it to let it in for good.
        <template v-if="configStaged">The allowlist waits until the staged change to config.yaml is applied or discarded.</template>
      </template>
      <template #footer>
        <UButton label="Allowlist and unban" :disabled="configStaged" @click="allowlist(asking!, true)" />
        <UButton color="neutral" variant="outline" label="Unban only" @click="unbanOnly(asking!)" />
        <UButton color="neutral" variant="ghost" label="Cancel" @click="asking = undefined" />
      </template>
    </UModal>
  </section>
</template>
