/** The History page: the snapshots of past applies, the diff to each, and restoring one into the staged changes. */
export function useHistory() {
  const api = useApi();
  const pending = usePending();
  // An apply refreshes this key (usePending), since it adds a snapshot.
  const { data: snapshots } = useAsyncData("history", async () => (await api.GET("/api/v1/history")).data ?? null);
  const diffs = ref<Record<string, string | null>>({});
  const opened = ref<string>();

  // Each diff is against the files as they are now, so a new apply makes the loaded ones stale.
  watch(snapshots, () => {
    diffs.value = {};
    opened.value = undefined;
  });

  async function toggle(id: string): Promise<void> {
    opened.value = opened.value === id ? undefined : id;
    if (!opened.value || typeof diffs.value[id] === "string") return;
    const { data } = await api.GET("/api/v1/history/{snapshot_id}", { params: { path: { snapshot_id: id } } });
    // A failed load is not cached, so opening the row again retries it.
    diffs.value = { ...diffs.value, [id]: data ? data.diff : null };
  }

  /** Stage a snapshot. Returns the API's refusal, or undefined when it was staged. */
  async function restore(id: string): Promise<string | undefined> {
    const { error } = await api.POST("/api/v1/history/{snapshot_id}/restore", { params: { path: { snapshot_id: id } } });
    if (error) return apiErrorMessage(error, "The snapshot was not restored.");
    await pending.refresh();
    return undefined;
  }

  return { snapshots, diffs, opened, toggle, restore };
}
