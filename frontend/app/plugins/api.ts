export default defineNuxtPlugin((nuxtApp) => {
  const api = createApiClient();
  // A session that ran out mid-page sends the admin to log in again. The auth endpoints answer 401
  // on purpose (no session yet, wrong password), so their callers handle it.
  api.use({
    onResponse({ request, response }) {
      if (response.status !== 401 || new URL(request.url).pathname.startsWith("/api/v1/auth/")) return;
      void nuxtApp.runWithContext(() => navigateTo("/login"));
    },
  });
  return { provide: { api } };
});
