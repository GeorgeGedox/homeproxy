/** `/status`, shared under one key by every page that shows it, and by the sidebar. */
export function useStatus() {
  const api = useApi();
  return useAsyncData("status", async () => (await api.GET("/api/v1/status")).data ?? null);
}
