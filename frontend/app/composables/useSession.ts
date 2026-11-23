/** The session check behind the auth guard, and logout. */
export function useSession() {
  const api = useApi();

  async function refresh(): Promise<SessionState> {
    // A valid session means setup is done, so only a visitor without one waits for the second request.
    const me = await api.GET("/api/v1/auth/me");
    if (me.response.ok) return "authenticated";
    const setup = await api.GET("/api/v1/setup/status");
    if (setup.data?.setup_required) return "setup";
    return me.response.status === 401 ? "anonymous" : "unreachable";
  }

  async function logout(): Promise<void> {
    await api.POST("/api/v1/auth/logout");
    await navigateTo("/login");
  }

  return { refresh, logout };
}
