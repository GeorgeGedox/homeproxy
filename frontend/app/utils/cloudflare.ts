import type { components } from "~~/api/schema";
import type { LedTone } from "~/utils/health";

export type CloudflareState = components["schemas"]["CloudflareState"];
type Zone = components["schemas"]["ZoneResult"];
type Change = components["schemas"]["Change"];

/** One site record of a zone, as it is now and as the next sync leaves it. */
export interface CloudflareRow {
  name: string;
  /** Where it points: the new target while it is added or changed, else the current one. */
  content: string;
  proxied: boolean;
  state: "live" | "adding" | "removing" | "changing" | "hand-made";
  /** The target or proxy setting it has now, while a change is waiting. */
  was?: string;
}

/** HomeProxy marks its DDNS record so in the record comment (docs/cloudflare-dns.md). */
const isDdnsRecord = (comment: string) => / ddns:/.test(comment);
const proxyWord = (proxied: boolean) => (proxied ? "proxied" : "DNS only");

/** A record as the waiting change leaves it. */
function changedRow(change: Change, now: CloudflareRow | undefined): CloudflareRow {
  const row = { name: change.name, content: change.content, proxied: change.proxied };
  if (change.action === "create") return { ...row, state: "adding" };
  if (change.action === "delete") return { ...row, ...now, state: "removing" };
  return { ...row, state: "changing", was: change.old_content !== change.content ? change.old_content : proxyWord(change.old_proxied) };
}

/** The zone's site records with the waiting changes and the hand-made conflicts merged in, by name. */
export function zoneRows(zone: Zone): CloudflareRow[] {
  const rows = new Map<string, CloudflareRow>();
  for (const record of zone.records ?? []) {
    if (!isDdnsRecord(record.comment)) rows.set(record.name, { name: record.name, content: record.content, proxied: record.proxied, state: "live" });
  }
  for (const change of zone.changes ?? []) rows.set(change.name, changedRow(change, rows.get(change.name)));
  for (const conflict of zone.conflicts ?? []) rows.set(conflict.name, { name: conflict.name, content: "", proxied: false, state: "hand-made" });
  return [...rows.values()].sort((a, b) => a.name.localeCompare(b.name));
}

/** A zone's errors from the dry run and from the last sync, each once: a sync that could not write answers 200 with them. */
export function zoneErrors(state: CloudflareState, zone: string): string[] {
  const results = [...state.zones, ...(state.last_reconcile?.zones ?? [])].filter((result) => result.zone === zone);
  return [...new Set(results.flatMap((result) => result.errors ?? []))];
}

const plural = (count: number, one: string, many: string) => (count === 1 ? one : `${count} ${many}`);

/** The page's light, worst first: off, a failure, something that needs the admin, changes waiting, in sync. */
export function cloudflareStatus(state: CloudflareState): { tone: LedTone; text: string } {
  if (!state.enabled) return { tone: "off", text: "Off" };
  if (state.last_reconcile?.error) return { tone: "down", text: "The last sync was refused" };
  const zones = state.zones;
  if (!zones.length) return { tone: "off", text: "No domain on Cloudflare" };
  const ddns = (...actions: string[]) => state.ddns.filter((result) => actions.includes(result.action)).length;
  const failures = zones.filter((zone) => zoneErrors(state, zone.zone).length).length + ddns("failed", "blocked");
  if (failures) return { tone: "down", text: plural(failures, "1 problem", "problems") };
  const handMade = zones.flatMap((zone) => zone.conflicts ?? []).length + ddns("hand-made");
  if (handMade) return { tone: "unknown", text: plural(handMade, "1 hand-made record needs you", "hand-made records need you") };
  const waiting = zones.flatMap((zone) => zone.changes ?? []).length;
  if (waiting) return { tone: "staged", text: plural(waiting, "1 record change waiting", "record changes waiting") };
  return { tone: "up", text: "In sync" };
}
