import type { components } from "~~/api/schema";
import { duration } from "~/utils/format";

export type AuditEntry = components["schemas"]["AuditEntry"];
type Details = Record<string, unknown>;

const text = (value: unknown) => (typeof value === "string" || typeof value === "number" ? String(value) : "");
const list = (value: unknown) => (Array.isArray(value) ? value.map(text) : []);
/** "crowdsec.bouncer_key" as "bouncer key". */
const field = (name: string) => (name.split(".").at(-1) ?? name).replaceAll("_", " ");

/** Each action that skips staging, in the admin's words. Applies are on the Applies tab instead. */
const WORDS: Record<string, (details: Details) => string> = {
  "credentials.replace": (d) => `Credentials ${text(d.name)} replaced`,
  "credentials.delete": (d) => `Credentials ${text(d.name)} deleted`,
  keys: (d) => `Keys changed: ${list(d.changed).map(field).join(", ")}`,
  // Target names are the admin's own, so they stay as typed; only the heartbeat field gets words.
  notification_urls: (d) => `Notifications changed: ${list(d.changed).map((name) => (name === "heartbeat_url" ? "heartbeat URL" : name)).join(", ")}`,
  dashboard_settings: (d) => `Session length set to ${text(d.session_ttl)}`,
  dashboard_listen: (d) => `Dashboard address set to ${text(d.listen)}`,
  "security.decision.add": (d) =>
    `${text(d.value)} ${d.type === "captcha" ? "sent to a captcha" : "banned"} for ${duration(Number(d.duration_seconds) || 0)}`,
  "security.decision.delete": (d) => (d.value ? `${text(d.value)} unbanned` : `Decision ${text(d.id)} removed`),
  "security.allowlist.add": (d) => `${text(d.entry)} always allowed`,
  "security.allowlist.remove": (d) => `${text(d.entry)} no longer always allowed`,
  "cloudflare.purge": (d) => `Cloudflare site records removed: ${list(d.deleted).length}`,
  "cloudflare.adopt": (d) => `DDNS record ${text(d.record)} adopted`,
};

/** The audit entries that are not applies, as plain words; an unknown action shows its name. */
export const activityText = (entry: AuditEntry): string => WORDS[entry.action]?.(entry.details ?? {}) ?? entry.action;
export const isActivity = (entry: AuditEntry): boolean => entry.action !== "apply";
