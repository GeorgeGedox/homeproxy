/**
 * A site is what the admin thinks of as one place: the `http` routes that share a host (directly or
 * through each other), or a single redirect, passthrough or tcp route. Only `http` routes may share a
 * host (`haproxy/apply_rules.py`), and they must agree on the host's settings, so those belong to the site.
 */
export interface Site {
  key: string;
  /** Every host of the site, in first-seen order. Empty for a tcp route. */
  hosts: string[];
  /** Its routes, shortest path first. */
  routes: Route[];
}

const hostsOf = (route: Route): string[] => (route.type === "tcp" ? [] : route.hosts);
const pathOf = (route: Route): string => (route.type === "http" ? route.path_prefix : "/");

/** Group routes into sites, ordered by their first host (tcp sites by port, last). */
export function groupSites(routes: Route[]): Site[] {
  const sites: Site[] = [];
  const byHost = new Map<string, Site>();
  for (const route of routes) {
    const joined = route.type === "http" ? [...new Set(route.hosts.map((host) => byHost.get(host)).filter((site) => site !== undefined))] : [];
    const site = joined[0] ?? { key: route.name, hosts: [], routes: [] };
    if (!joined.length) sites.push(site);
    for (const other of joined.slice(1)) {
      site.routes.push(...other.routes);
      site.hosts.push(...other.hosts.filter((host) => !site.hosts.includes(host)));
      sites.splice(sites.indexOf(other), 1);
    }
    site.routes.push(route);
    site.hosts.push(...hostsOf(route).filter((host) => !site.hosts.includes(host)));
    if (route.type === "http") for (const host of site.hosts) byHost.set(host, site);
  }
  for (const site of sites) site.routes.sort((a, b) => pathOf(a).length - pathOf(b).length || pathOf(a).localeCompare(pathOf(b)));
  return sites.sort((a, b) => (sortKey(a) < sortKey(b) ? -1 : 1));
}

/** Hosts alphabetically, then tcp sites by port: "~" sorts after every host character. */
const sortKey = (site: Site) => site.hosts[0] ?? `~${String(site.routes[0]?.type === "tcp" ? site.routes[0].listen_port : "").padStart(5, "0")}`;

/** The hosts a route serves when they are not all of its site's, so `b` never looks like it has `/api`. */
export function onlyOn(route: Route, site: Site): string[] {
  const hosts = hostsOf(route);
  return hosts.length < site.hosts.length ? hosts : [];
}

/** The sites under one domain, or under "TCP" for tcp sites. */
export interface SiteGroup {
  name: string;
  sites: Site[];
}

/** The domain a host belongs to: the longest zone that holds it, as the backend picks it. */
export const zoneOf = (host: string, zones: string[]): string | undefined =>
  zones.filter((zone) => host === zone || host.endsWith(`.${zone}`)).sort((a, b) => b.length - a.length)[0];

/** Group sites under the domain of their first host (the longest zone that holds it), the apex site first. TCP comes last. */
export function groupByZone(sites: Site[], zones: string[]): SiteGroup[] {
  const groups = new Map<string, SiteGroup>();
  for (const site of sites) {
    const host = site.hosts[0];
    const zone = host === undefined ? "TCP" : (zoneOf(host, zones) ?? "Other");
    const group = groups.get(zone);
    if (group) group.sites.push(site);
    else groups.set(zone, { name: zone, sites: [site] });
  }
  const last = (name: string) => ["Other", "TCP"].indexOf(name);
  for (const group of groups.values()) group.sites.sort((a, b) => Number(b.hosts[0] === group.name) - Number(a.hosts[0] === group.name));
  return [...groups.values()].sort((a, b) => last(a.name) - last(b.name) || a.name.localeCompare(b.name));
}
