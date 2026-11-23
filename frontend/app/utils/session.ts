export type SessionState = "setup" | "anonymous" | "authenticated" | "unreachable";

/** Paths that need no session. */
const PUBLIC_PATHS = ["/login", "/setup"];

/**
 * Decide where the auth guard sends the visitor.
 * Returns `null` to allow the navigation.
 */
export function guardRedirect(state: SessionState, path: string): string | null {
  switch (state) {
    case "setup":
      return path === "/setup" ? null : "/setup";
    case "anonymous":
      return path === "/login" ? null : "/login";
    case "authenticated":
      return PUBLIC_PATHS.includes(path) ? "/" : null;
    case "unreachable":
      return null;
  }
}
