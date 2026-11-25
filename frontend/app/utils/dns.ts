import type { components } from "~~/api/schema";

type DnsRecord = components["schemas"]["DnsRecord"];

/** One name the DNS server answers, or will answer after the next apply. */
export interface DnsName {
  name: string;
  /** The address it answers with: the staged one, or the live one while its removal is staged. */
  target: string;
  /** The route that owns the name, or undefined for an extra record. */
  route?: string;
  state: "live" | "staged" | "removing";
  /** The address it answers with now, while a different one is staged. */
  was?: string;
}

/** The address a route's hosts get: HomeProxy's LAN address, or the route's own DNS target. */
function dnsTarget(route: Route, lanIp: string): string {
  const target = route.type === "tcp" ? undefined : route.dns?.target;
  return !target || target === "haproxy" ? lanIp : target;
}

/**
 * Every name the DNS server answers, from the config with staged edits and from the table it serves now.
 * An enabled route's host wins over an extra record of the same name, as on the server.
 */
export function dnsNames(routes: Route[], extra: DnsRecord[], lanIp: string, live: Record<string, string>): DnsName[] {
  const staged = new Map<string, { target: string; route?: string }>();
  for (const route of routes) {
    if (route.type === "tcp" || !route.enabled) continue;
    for (const host of route.hosts) staged.set(host, { target: dnsTarget(route, lanIp), route: route.name });
  }
  for (const record of extra) if (!staged.has(record.name)) staged.set(record.name, { target: record.target });
  return [...new Set([...staged.keys(), ...Object.keys(live)])].sort().map((name) => {
    const next = staged.get(name);
    if (!next) return { name, target: live[name] ?? "", state: "removing" };
    const now = live[name];
    if (now === next.target) return { name, ...next, state: "live" };
    return { name, ...next, state: "staged", ...(now && { was: now }) };
  });
}

/** The forwarding lines for every zone: AdGuard Home upstream syntax, and dnsmasq syntax (used by Pi-hole). `listen` is `ip:port`. */
export function forwardingLines(zones: string[], listen: string): { adguard: string; dnsmasq: string } {
  const at = listen.lastIndexOf(":");
  const dnsmasqListen = at < 0 ? listen : `${listen.slice(0, at)}#${listen.slice(at + 1)}`;
  return {
    adguard: zones.map((zone) => `[/${zone}/]${listen}`).join("\n"),
    dnsmasq: zones.map((zone) => `server=/${zone}/${dnsmasqListen}`).join("\n"),
  };
}
