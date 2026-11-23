/** The states of a status light: StatusLed draws each one. */
export type LedTone = "up" | "down" | "unknown" | "staged" | "off";

/** HAProxy's server states as status lights; a route without a health check, or switched off, is grey, not a warning. */
const TONES: Record<string, LedTone> = { UP: "up", DOWN: "down", "no check": "off", MAINT: "off" };
const WORDS: Record<string, string> = { UP: "Up", DOWN: "Down", "no check": "Not checked", MAINT: "Disabled" };

export const healthTone = (state: string): LedTone => TONES[state] ?? "unknown";
export const healthText = (state: string): string => WORDS[state] ?? state;

/** HAProxy's version without its build suffix, such as "3.4.7", or undefined while it does not answer. */
export function haproxyVersion(status?: { haproxy_version?: string | null; haproxy_error?: string | null } | null): string | undefined {
  if (!status?.haproxy_version || status.haproxy_error) return undefined;
  return status.haproxy_version.split("-")[0];
}
