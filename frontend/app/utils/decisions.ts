import type { components } from "~~/api/schema";

/** Blocklist origins: an unban lasts only until the next pull, so the allowlist is offered. */
export const FROM_BLOCKLIST = ["CAPI", "lists"];

/** The lengths a ban or captcha can have. */
export const DURATIONS = [
  { label: "1 hour", value: 3600 },
  { label: "1 day", value: 86400 },
  { label: "7 days", value: 604800 },
  { label: "30 days", value: 2592000 },
];
const ORIGINS: Record<string, string> = { crowdsec: "Detected here", cscli: "Added by hand", CAPI: "Community blocklist", lists: "Blocklist" };
/** Where a decision came from, in words. */
export const originName = (origin: string) => ORIGINS[origin] ?? origin;

/** The CrowdSec line on Overview: your own bans and captchas, and whether a community list is loaded. */
export function banSummary({ bans, captchas, blocklist }: components["schemas"]["DecisionCounts"]): string {
  const parts = [bans === 1 ? "1 ban" : `${bans} bans`];
  if (captchas) parts.push(captchas === 1 ? "1 captcha" : `${captchas} captchas`);
  if (blocklist) parts.push("community list");
  return parts.join(" · ");
}
