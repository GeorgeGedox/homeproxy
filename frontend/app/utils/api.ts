import createClient from "openapi-fetch";
import type { paths } from "~~/api/schema";

/** Create the typed API client. All UI code calls the backend through this. */
export function createApiClient(baseUrl = "") {
  const client = createClient<paths>({ baseUrl, credentials: "same-origin" });
  // A request that never reached the API answers like an API error, so every caller already handles it.
  // 502, because 503 from the API itself means a part is unavailable (metrics) and callers check for it.
  client.use({ onError: () => Response.json({ error: { code: "unreachable", message: "The server is not reachable." } }, { status: 502 }) });
  return client;
}

export type ApiClient = ReturnType<typeof createApiClient>;

/** The `error.message` of an API error body, or `fallback` when there is none. */
export function apiErrorMessage(error: unknown, fallback: string): string {
  const body = error as { error?: { message?: string } } | undefined;
  return body?.error?.message ?? fallback;
}

/** The API field up to its first list index or key, which is the form field that holds the value. */
function formField(field: string): string {
  const segments = field.split(".");
  const end = segments.findIndex((segment) => /^[0-9]+$/.test(segment) || segment === "<key>");
  return segments.slice(0, end === -1 ? undefined : end).join(".");
}

const sentence = (text: string) => `${text.charAt(0).toUpperCase()}${text.slice(1)}${text.endsWith(".") ? "" : "."}`;

/**
 * The first error of an API error body, for its form field. `field` is the API field up to its
 * first list index or key, so `hosts.0` lands on `hosts`. `message` is that error's text alone.
 */
export function apiFieldError(error: unknown, fallback: string): { field?: string; message: string } {
  const body = error as { error?: { message?: string; field?: string | null } } | undefined;
  const message = body?.error?.message ?? fallback;
  const field = body?.error?.field;
  if (!field) return { message };
  // A validation message lists every error as `field: text`, joined by "; ".
  const text = message.startsWith(`${field}: `) ? message.slice(field.length + 2).split("; ")[0] : message;
  return { field: formField(field), message: sentence(text ?? message) };
}
