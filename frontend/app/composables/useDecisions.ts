import type { components } from "~~/api/schema";

export type Decision = components["schemas"]["Decision"];
type Call = () => Promise<{ response: Response; error?: unknown }>;


/** CrowdSec's active decisions: load, ban, extend, unban, and allowlist an address. */
export function useDecisions() {
  const api = useApi();
  // A blocklist can hold thousands of rows; the list is only ever replaced, so it needs no deep reactivity.
  const decisions = shallowRef<Decision[]>([]);
  const error = ref("");
  const loaded = ref(false);
  // The API answers 409 crowdsec_not_configured while CrowdSec is off or lacks its machine login.
  const unconfigured = ref(false);

  async function load(): Promise<void> {
    const { data, error: failure } = await api.GET("/api/v1/security/decisions");
    unconfigured.value = (failure as { error?: { code?: string } } | undefined)?.error?.code === "crowdsec_not_configured";
    decisions.value = data ?? [];
    error.value = data || unconfigured.value ? "" : apiErrorMessage(failure, "The decisions could not be loaded.");
    loaded.value = true;
  }

  /** Run one call, show its failure, then reload the list. */
  async function act(call: Call, fallback: string): Promise<boolean> {
    const { response, error: failure } = await call();
    await load();
    // After the reload, which clears the error of the last load.
    if (!response.ok) error.value = apiErrorMessage(failure, fallback);
    return response.ok;
  }

  const removeCall = (item: Decision): Call => () => api.DELETE("/api/v1/security/decisions/{decision_id}", { params: { path: { decision_id: item.id } } });
  const unban = (item: Decision) => act(removeCall(item), "The decision was not removed.");
  const ban = (body: { value: string; type: "ban" | "captcha"; duration_seconds: number }) => act(() => api.POST("/api/v1/security/decisions", { body }), "The decision was not added.");

  // The LAPI cannot change when a decision ends, so Extend adds the longer one, then removes the old one.
  async function extend(item: Decision, seconds: number): Promise<void> {
    if (!(await ban({ value: item.value, type: item.type as "ban" | "captcha", duration_seconds: seconds }))) return;
    await act(removeCall(item), "The longer decision was added, but the old one was not removed.");
  }
  /** The durations that make a decision longer; a shorter one would only add a row. */
  const longerThan = (item: Decision) => DURATIONS.filter(({ value }) => value > (new Date(item.expires_at).getTime() - Date.now()) / 1000);

  const allowlist = (item: Decision) => act(() => api.POST("/api/v1/security/allowlist", { body: { entry: item.value } }), "The address was not allowlisted.");
  /** Allowlist the address, then unban it: a blocklist decision would come back at the next pull.
   * True once the address is allowlisted, even when the unban then fails. */
  async function allowlistAndUnban(item: Decision): Promise<boolean> {
    if (!(await allowlist(item))) return false;
    await act(removeCall(item), "The address is allowlisted, but its decision was not removed.");
    return true;
  }

  /** "12 bans, 2 captchas, 1,204 from blocklists". */
  const summary = computed(() => {
    const count = (type: string) => decisions.value.filter((item) => item.type === type).length;
    const listed = decisions.value.filter((item) => FROM_BLOCKLIST.includes(item.origin)).length;
    const [bans, captchas] = [count("ban"), count("captcha")];
    return [
      bans === 1 ? "1 ban" : `${formatCount(bans)} bans`,
      ...(captchas ? [captchas === 1 ? "1 captcha" : `${formatCount(captchas)} captchas`] : []),
      ...(listed ? [`${formatCount(listed)} from blocklists`] : []),
    ].join(", ");
  });

  onMounted(() => void load());
  return { decisions, error, loaded, unconfigured, summary, load, ban, unban, extend, longerThan, allowlist, allowlistAndUnban };
}
