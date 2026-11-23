/** The shared typed API client. */
export function useApi(): ApiClient {
  return useNuxtApp().$api;
}
