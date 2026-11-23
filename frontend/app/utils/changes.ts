import type { components } from "~~/api/schema";

export type ChangeSummary = components["schemas"]["ChangeSummary"];

/** Names for the `config.yaml` sections the backend summarizes as settings. */
const SECTIONS: Record<string, string> = {
  network: "Network settings",
  dns_server: "DNS server settings",
  dns_records: "Extra DNS records",
  cloudflare: "Cloudflare settings",
  geoip: "GeoIP settings",
  crowdsec: "CrowdSec settings",
  certificates: "Certificate settings",
  haproxy: "HAProxy settings",
  prometheus: "Prometheus settings",
  notifications: "Notification settings",
};

/** One staged change in plain words, such as "Route example.com/test added". */
export function changeText(change: ChangeSummary): string {
  switch (change.kind) {
    case "route": return `Route ${change.label} ${change.action}`;
    case "domain": return `Domain ${change.label} ${change.action}`;
    case "settings": return `${SECTIONS[change.label] ?? `${change.label} settings`} ${change.action}`;
    case "basic_auth": return `Basic auth users changed on ${change.label}`;
    default: return `${change.label} ${change.action}`;
  }
}

export const CHANGE_ICONS: Record<ChangeSummary["action"], string> = {
  added: "lucide:plus",
  changed: "lucide:pencil",
  removed: "lucide:minus",
};

export type SnapshotInfo = components["schemas"]["SnapshotInfo"];

/** Messages of snapshots that have no plain-words summary: live writes and applies of the saved files. */
const MESSAGES: Record<string, string> = {
  "apply saved config": "Saved files applied again",
  "edit crowdsec.allowlist": "Allowlist changed",
  "edit dashboard.listen": "Dashboard address changed",
  "edit dashboard.session_ttl": "Session length changed",
};
const VERBS: Record<string, ChangeSummary["action"]> = { add: "added", edit: "changed", delete: "removed" };

/** One part of an older snapshot's file message, such as "add routes/app.yaml", as a change. */
function fileChange(part: string): ChangeSummary | undefined {
  const [verb = "", path = ""] = part.split(" ");
  const action = VERBS[verb];
  if (!action || !path) return undefined;
  const name = path.replace(/^\w+\//, "").replace(/\.yaml$/, "");
  if (path === "config.yaml") return { kind: "file", label: "Settings", action: "changed" };
  if (path.startsWith("routes/")) return { kind: "route", label: name, action };
  if (path.startsWith("domains/")) return { kind: "domain", label: name, action };
  return { kind: "file", label: path, action };
}

/** What a snapshot changed, in plain words, naming at most three changes. */
export function snapshotText(snapshot: SnapshotInfo): string {
  const known = MESSAGES[snapshot.message];
  if (!snapshot.changes.length && known) return known;
  const parts = snapshot.message.split("; ").map(fileChange);
  const changes = snapshot.changes.length ? snapshot.changes : parts.every(Boolean) ? (parts as ChangeSummary[]) : [];
  if (!changes.length) return snapshot.message;
  const texts = changes.slice(0, 3).map(changeText);
  return changes.length > 3 ? `${texts.join(", ")} and ${changes.length - 3} more` : texts.join(", ");
}
