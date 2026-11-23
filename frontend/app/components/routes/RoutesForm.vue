<script setup lang="ts">
import type { RouteUser } from "~/utils/route";

const props = defineProps<{
  /** The route being edited, for its internal ID. */
  original?: Route;
  /** A new path on a site: only websites share a host, so the type is fixed. */
  addingPath: boolean;
  /** The other paths of the draft's site. */
  siteOthers: Route[];
  siteChanged: boolean;
  lanIp: string;
  cloudflareAvailable: boolean;
  saving: boolean;
  error: string;
  /** An API error body, shown at the field it names. */
  failure?: unknown;
  problems: { errors: string[]; warnings: string[] };
}>();
defineEmits<{ submit: [] }>();
const draft = defineModel<RouteDraft>("draft", { required: true });
const users = defineModel<RouteUser[]>("users", { required: true });
const formRef = useTemplateRef("formRef");
const { error: refusedError, show: showRefused, clear: clearRefused } = useFormError(formRef);
watch(() => props.failure, (failure) => (failure ? showRefused(failure, "The route was not staged.") : clearRefused()));
</script>

<template>
  <UForm ref="formRef" :state="draft" :validate-on="[]" class="max-w-2xl space-y-6" @submit="$emit('submit')">
    <RoutesFormWhere v-model="draft" :adding-path="addingPath" />
    <RoutesFormTarget v-model="draft" />
    <RoutesFormSite v-model="draft" :others="siteOthers" :changed="siteChanged" :lan-ip="lanIp" :cloudflare-available="cloudflareAvailable" />
    <RoutesFormProtection v-model="draft" v-model:users="users" />
    <RoutesFormAdvanced v-if="draft.type === 'http'" v-model="draft" :failure="failure" />

    <div class="space-y-3 border-t border-default pt-6">
      <UAlert v-if="problems.errors.length" color="error" variant="subtle" title="Fix before staging">
        <template #description>
          <ul class="space-y-1"><li v-for="problem in problems.errors" :key="problem">{{ problem }}</li></ul>
        </template>
      </UAlert>
      <p v-for="warning in problems.warnings" :key="warning" class="flex items-start gap-3 text-sm text-muted">
        <UIcon name="lucide:triangle-alert" class="mt-0.5 size-4 shrink-0 text-warning" />{{ warning }}
      </p>
      <UAlert v-if="error" color="error" variant="subtle" :description="error" />
      <UAlert v-if="refusedError" color="error" variant="subtle" :description="refusedError" />
      <div class="flex flex-wrap items-center gap-2">
        <UButton type="submit" :loading="saving" label="Stage change" />
        <UButton to="/routes" color="neutral" variant="ghost" label="Cancel" />
        <p v-if="original" class="ml-auto text-xs text-dimmed">
          Internal ID <span class="font-mono text-muted">{{ original.name }}</span>, used in logs, metrics and alerts
        </p>
      </div>
    </div>
  </UForm>
</template>
