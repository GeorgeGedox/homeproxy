<script setup lang="ts">
const api = useApi();
const pending = usePending();
const entries = ref<string[]>([]);
const entry = ref("");
const formRef = useTemplateRef("formRef");
const { error, show, clear } = useFormError(formRef);
const warnings = ref<string[]>([]);

async function load(): Promise<void> {
  const { data, error: failure } = await api.GET("/api/v1/security/allowlist");
  if (data) entries.value = data;
  else error.value = apiErrorMessage(failure, "The allowlist could not be loaded.");
}

/** A 502 means the change is saved but HAProxy missed it, so the API message asks to send it again. */
async function act(call: () => Promise<{ response: Response; error?: unknown }>, fallback: string): Promise<void> {
  warnings.value = [];
  const { response, error: failure } = await call();
  clear();
  if (response.ok) entry.value = "";
  else show(failure, fallback);
  await load();
}

async function post(): Promise<{ response: Response; error?: unknown }> {
  const result = await api.POST("/api/v1/security/allowlist", { body: { entry: entry.value.trim() } });
  warnings.value = result.data?.warnings ?? [];
  return result;
}

const add = () => act(post, "The entry was not added.");
const remove = (value: string) => act(() => api.DELETE("/api/v1/security/allowlist/{entry}", { params: { path: { entry: value } } }), "The entry was not removed.");

defineExpose({ load });
onMounted(() => void load());
</script>

<template>
  <section class="rounded-lg border border-default" aria-label="Always allowed">
    <header class="rounded-t-lg border-b border-default bg-muted px-4 py-3">
      <h2 class="text-sm text-highlighted">Always allowed <span v-if="entries.length" class="font-mono text-xs text-dimmed">{{ entries.length }}</span></h2>
      <p class="text-xs text-muted">CrowdSec never bans or challenges these. Your LAN is always allowed. Changes take effect at once.</p>
    </header>
    <ul v-if="entries.length" class="divide-y divide-default border-b border-default">
      <li v-for="item in entries" :key="item" class="flex items-center gap-3 px-4 py-1.5 text-sm">
        <span class="min-w-0 flex-1 truncate font-mono text-highlighted">{{ item }}</span>
        <UButton icon="lucide:x" color="neutral" variant="ghost" size="xs" :aria-label="`Remove ${item}`" @click="remove(item)" />
      </li>
    </ul>
    <UForm ref="formRef" :state="{ entry }" :validate-on="[]" :disabled="pending.staged.value" class="space-y-2 px-4 py-3" @submit="add">
      <UFormField name="entry" :description="pending.staged.value ? 'Apply or discard the staged changes first.' : undefined">
        <div class="flex gap-2">
          <UInput v-model="entry" placeholder="192.168.1.0/24" aria-label="Address or range to allow" class="min-w-0 flex-1 sm:max-w-xs" />
          <UButton type="submit" icon="lucide:plus" label="Allow" color="neutral" variant="outline" />
        </div>
      </UFormField>
      <p v-if="error" class="text-sm text-error">{{ error }}</p>
      <p v-for="warning in warnings" :key="warning" class="flex items-start gap-2 text-sm text-muted">
        <UIcon name="lucide:triangle-alert" class="mt-0.5 size-4 shrink-0 text-warning" />{{ warning.replace(/^crowdsec\.allowlist: /, "") }}
      </p>
    </UForm>
  </section>
</template>
