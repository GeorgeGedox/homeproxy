import type { components } from "~~/api/schema";

type Review = components["schemas"]["PendingReview"];

export interface ApplyFailure {
  stage: string;
  errors: string[];
  /** Whether staged edits were kept, so the bar can say so. */
  staged: boolean;
}

/** The staged change set behind the pending bar: refresh it, apply it or discard it. */
export function usePending() {
  const api = useApi();
  const toast = useToast();
  const review = useState<Review | null>("pending-review", () => null);
  const applying = useState("pending-applying", () => false);
  const failure = useState<ApplyFailure | undefined>("pending-failure", () => undefined);
  /** Count applies and discards, for pages that keep their own copy of live or staged state and reload after one. */
  const applies = useState("pending-applies", () => 0);
  const discards = useState("pending-discards", () => 0);

  /** Changes in plain words; their number is what the bar counts. */
  const summary = computed(() => review.value?.summary ?? []);
  const count = computed(() => summary.value.length);
  /** Staged files; a change with no plain-words summary, such as a comment edit, still counts. */
  const staged = computed(() => Boolean(review.value?.changes.length));
  /** Nothing staged, but the saved files differ from what HAProxy runs, or do not render: a hand edit to load or fix. */
  const handEdit = computed(() => !review.value?.changes.length && Boolean(review.value?.generated_diff || review.value?.generated_error));

  async function refresh(): Promise<void> {
    review.value = (await api.GET("/api/v1/changes")).data ?? null;
  }

  /** The Cloudflare reconcile runs after the apply. Its failure never fails the apply. */
  async function followCloudflare(id: string): Promise<void> {
    const job = await waitForJob(api, id);
    if (job?.state === "finished") return;
    toast.add({ title: "Cloudflare records not updated", description: job?.error ?? "Unknown error", color: "warning" });
  }

  async function apply(): Promise<boolean> {
    const wasStaged = staged.value;
    applying.value = true;
    failure.value = undefined;
    try {
      const { data, error } = await api.POST("/api/v1/apply");
      if (!data) {
        failure.value = { stage: "request", errors: [apiErrorMessage(error, "The apply request failed.")], staged: wasStaged };
      } else if (data.success) {
        // The warnings stay listed on Overview (`/status` last_apply), so the toast only counts them.
        toast.add(data.warnings.length
          ? { title: `Applied with ${warningCount(data.warnings)}`, color: "warning", actions: [{ label: "View on Overview", to: "/", color: "neutral", variant: "outline" }] }
          : { title: "Applied", color: "success" });
        if (data.cloudflare_job) void followCloudflare(data.cloudflare_job);
      } else {
        failure.value = { stage: data.stage, errors: [...data.errors, ...haproxyAlerts(data.haproxy_log)], staged: wasStaged };
      }
    } finally {
      applying.value = false;
    }
    await Promise.all([refresh(), refreshNuxtData(["status", "history"])]);
    if (!failure.value) applies.value += 1;
    return !failure.value;
  }

  async function discard(): Promise<void> {
    const { error } = await api.DELETE("/api/v1/changes");
    if (error) toast.add({ title: "Nothing was discarded", description: apiErrorMessage(error, "The discard request failed."), color: "error" });
    else {
      failure.value = undefined;
      discards.value += 1;
    }
    await refresh();
  }

  return { review, summary, count, staged, handEdit, applying, failure, applies, discards, refresh, apply, discard };
}

/** HAProxy's output holds notices and warnings too; its alert lines say why it refused. */
function haproxyAlerts(log: string): string[] {
  const text = log.trim();
  const alerts = text.split("\n").filter((line) => line.includes("[ALERT]"));
  return alerts.length ? alerts : text ? [text] : [];
}
