<script setup lang="ts">
import type { components } from "~~/api/schema";

type CheckResult = components["schemas"]["CheckResult"];

const crowdsec = defineModel<components["schemas"]["Crowdsec"]>({ required: true });
const api = useApi();
const { state, appsec } = useBouncerState();
const { isSet } = useSettingsKeys();
// A set key shows even while captcha is off, so it can be seen and cleared.
const captcha = computed(() => crowdsec.value.captcha_provider !== "none" || ["captcha_site_key", "captcha_secret_key", "captcha_signing_key"].some((key) => isSet("crowdsec", key)));
const bouncer = computed(() => {
  if (state.value === "up") return { color: "success", label: "Bouncer up", hint: "" } as const;
  if (state.value === "starting") return { color: "info", label: "Bouncer starting", hint: "It was applied a moment ago and should be up in a few seconds." } as const;
  if (state.value === "down") return { color: "error", label: "Bouncer down", hint: "Requests are served without CrowdSec checks. Check the CrowdSec API URL and the bouncer key." } as const;
  return null;
});
const appsecBadge = computed(() => {
  if (appsec.value === "up") return { color: "success", label: "AppSec up" } as const;
  if (appsec.value === "down") return { color: "error", label: "AppSec down" } as const;
  return null;
});
const STOPS = "CrowdSec protection stops at the next apply.";

const checks = ref<CheckResult[]>([]);
const checkError = ref("");
const checking = ref(false);
const CHECK_ICONS = { true: "lucide:circle-check", false: "lucide:circle-x", null: "lucide:circle-minus" } as const;

/** Test the applied settings and the saved keys, one line for each part of CrowdSec. */
async function testConnection(): Promise<void> {
  checking.value = true;
  const { data, error } = await api.POST("/api/v1/security/crowdsec/check");
  checks.value = data ?? [];
  checkError.value = data ? "" : apiErrorMessage(error, "The connection test did not run.");
  checking.value = false;
}
// A key only matters while the feature that uses it is on, so only then does clearing it need a warning.
const needs = (what: string) => (crowdsec.value.enabled ? `${what} ${STOPS}` : undefined);
</script>

<template>
  <SettingsSection id="crowdsec" title="CrowdSec" description="Blocks addresses the CrowdSec engine bans. Keys are saved at once; everything else waits for Stage changes. docs/crowdsec.md sets up the engine.">
    <div class="flex flex-wrap items-center gap-3">
      <USwitch v-model="crowdsec.enabled" label="CrowdSec" />
      <UBadge v-if="bouncer" :color="bouncer.color" variant="subtle" :label="bouncer.label" data-testid="bouncer-state" />
      <UBadge v-if="appsecBadge" :color="appsecBadge.color" variant="subtle" :label="appsecBadge.label" />
    </div>
    <p v-if="bouncer?.hint" class="text-sm text-muted">{{ bouncer.hint }}</p>
    <p v-if="appsec === 'down'" class="text-sm text-muted">AppSec does not check requests, so they pass without it. Check the AppSec URL and the bouncer key.</p>
    <div class="space-y-2">
      <div class="flex flex-wrap items-center gap-3">
        <UButton label="Test connection" color="neutral" variant="outline" :loading="checking" @click="testConnection" />
        <span class="text-xs text-muted">Tests the applied settings and the saved keys.</span>
      </div>
      <p v-if="checkError" class="text-sm text-error">{{ checkError }}</p>
      <ul v-if="checks.length" class="space-y-1 text-sm" aria-label="Connection test">
        <li v-for="check in checks" :key="check.part" class="flex items-start gap-2">
          <UIcon :name="CHECK_ICONS[`${check.ok}`]" class="mt-0.5 size-4 shrink-0" :class="check.ok ? 'text-success' : check.ok === false ? 'text-error' : 'text-muted'" />
          {{ check.message }}
        </li>
      </ul>
    </div>
    <template v-if="crowdsec.enabled">
      <UFormField label="CrowdSec API URL" name="crowdsec.lapi_url" help="The engine's LAPI. The bouncer syncs its decisions from it every 10 seconds, and Security lists them."><UInput :model-value="crowdsec.lapi_url ?? ''" placeholder="http://127.0.0.1:18080" class="w-full" @update:model-value="crowdsec.lapi_url = $event || null" /></UFormField>
      <UFormField label="CrowdSec syslog address" name="crowdsec.syslog" hint="ip:port" help="Where HAProxy sends its access log for the engine to read."><UInput :model-value="crowdsec.syslog ?? ''" placeholder="127.0.0.1:15140" class="w-full" @update:model-value="crowdsec.syslog = $event || null" /></UFormField>
    </template>
    <SettingsKeyField section="crowdsec" name="bouncer_key" label="Bouncer key" :needed="needs('The bouncer cannot read decisions.')" />
    <SettingsKeyField section="crowdsec" name="machine_id" label="Machine ID" :needed="needs('The dashboard cannot list or change decisions.')" />
    <SettingsKeyField section="crowdsec" name="machine_password" label="Machine password" :needed="needs('The dashboard cannot list or change decisions.')" />
    <UFormField label="Captcha provider" help="Without one, a captcha decision blocks the client like a ban."><USelect v-model="crowdsec.captcha_provider" :items="['none', 'turnstile', 'hcaptcha', 'recaptcha']" class="w-full" /></UFormField>
    <template v-if="captcha">
      <SettingsKeyField section="crowdsec" name="captcha_site_key" label="Captcha site key" needed="Captcha challenges cannot be shown, so captcha decisions stop working." />
      <SettingsKeyField section="crowdsec" name="captcha_secret_key" label="Captcha secret key" needed="Captcha answers cannot be checked, so captcha decisions stop working." />
      <SettingsKeyField section="crowdsec" name="captcha_signing_key" label="Captcha signing key" description="Signs the cookie that lets a client through after it solves a captcha. Use random text of at least 32 characters, such as the output of openssl rand -base64 32." needed="Captcha passes cannot be signed, so captcha decisions stop working." />
    </template>
    <USwitch v-model="crowdsec.appsec" label="AppSec (WAF)" description="With the default hub scenario, CrowdSec bans an address for 4 h after it trips two different AppSec rules within about a minute. See docs/crowdsec.md." />
    <UFormField v-if="crowdsec.appsec" label="AppSec URL" name="crowdsec.appsec_url" hint="Empty means http://<CrowdSec API host>:7422">
      <UInput :model-value="crowdsec.appsec_url ?? ''" class="w-full" @update:model-value="crowdsec.appsec_url = $event || null" />
    </UFormField>
  </SettingsSection>
</template>
