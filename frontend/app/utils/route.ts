import type { components } from "~~/api/schema";

/** A basic auth user in the editor. A stored one has no password unless the admin sets a new one. */
export interface RouteUser {
  name: string;
  password: string;
  existing: boolean;
}

export type Route =
  | components["schemas"]["HttpRoute"]
  | components["schemas"]["PassthroughRoute"]
  | components["schemas"]["TcpRoute"]
  | components["schemas"]["RedirectRoute"];
export type RouteType = Route["type"];

export const ROUTE_TYPES: { label: string; description: string; value: RouteType }[] = [
  { label: "Website", description: "HTTP and HTTPS, with HomeProxy's certificate", value: "http" },
  { label: "Redirect", description: "Sends visitors to another URL", value: "redirect" },
  { label: "TLS passthrough", description: "The service keeps its own certificate", value: "passthrough" },
  { label: "TCP port", description: "Any TCP service, on a port of its own", value: "tcp" },
];

/** What the admin sees for a route: its hosts plus the path, or "Port 25565" for a tcp route. */
export function routeTarget(route: Route): string {
  if (route.type === "tcp") return `Port ${route.listen_port}`;
  const path = route.type === "http" && route.path_prefix !== "/" ? route.path_prefix : "";
  return route.hosts.map((host) => host + path).join(", ");
}

/** The editor fields each route type has, besides name, type, exposure and enabled. */
type RouteField = "hosts" | "path" | "upstreams" | "health_check" | "tls_upstream" | "listen_port" | "redirect";
const TYPE_FIELDS: Record<RouteType, RouteField[]> = {
  http: ["hosts", "path", "upstreams", "health_check", "tls_upstream"],
  passthrough: ["hosts", "upstreams"],
  tcp: ["listen_port", "upstreams"],
  redirect: ["hosts", "redirect"],
};

/** Whether the editor shows `field` for a route of this type. */
export function hasField(type: RouteType, field: RouteField): boolean {
  return TYPE_FIELDS[type].includes(field);
}

/** Whether the draft has every field its type needs. Until then a check would only list the empty fields. */
export function draftIsComplete(draft: RouteDraft): boolean {
  const filled = { hosts: draft.hosts.length > 0, upstreams: draft.upstreams.length > 0, listen_port: !!draft.listen_port, redirect: !!draft.redirect_to };
  return Object.entries(filled).every(([field, ok]) => ok || !hasField(draft.type, field as RouteField));
}

export interface RouteDraft {
  type: RouteType;
  hosts: string[];
  listen_port: number | undefined;
  exposure: "lan" | "public";
  upstreams: string[];
  health_path: string;
  tls_enabled: boolean;
  tls_verify: boolean;
  websocket: boolean;
  auth: "none" | "basic";
  request_headers: HeaderRow[];
  response_headers: HeaderRow[];
  path_prefix: string;
  strip_prefix: boolean;
  redirect_to: string;
  redirect_code: 301 | 302 | 307 | 308;
  keep_path: boolean;
  /** "haproxy", or an IPv4 address the internal DNS server answers for the hosts. */
  dns_target: string;
  cloudflare_dns: boolean;
  /** null follows the zone's default; passthrough routes need true or false. */
  cloudflare_proxied: boolean | null;
  block_countries: string[];
  /** Whether the route's own country list is applied. Off keeps the list for later. */
  geoip_enabled: boolean;
  enabled: boolean;
}

/** A new HTTP route with the defaults the form starts from. */
export function blankDraft(): RouteDraft {
  return {
    type: "http", hosts: [], listen_port: undefined, exposure: "lan", upstreams: [],
    health_path: "", tls_enabled: false, tls_verify: true, websocket: false, auth: "none", request_headers: [], response_headers: [], enabled: true,
    path_prefix: "/", strip_prefix: false, redirect_to: "", redirect_code: 301, keep_path: false,
    dns_target: "haproxy", cloudflare_dns: false, cloudflare_proxied: null, block_countries: [], geoip_enabled: false,
  };
}

type HeaderRow = { name: string; value: string };

const headerRows = (headers: Record<string, string> = {}): HeaderRow[] =>
  Object.entries(headers).map(([name, value]) => ({ name, value }));

const headerRecord = (rows: HeaderRow[]): Record<string, string> =>
  Object.fromEntries(rows.filter((h) => h.name).map((h) => [h.name, h.value]));

function httpToDraft(route: components["schemas"]["HttpRoute"]): Partial<RouteDraft> {
  return {
    health_path: route.health_check?.path ?? "",
    tls_enabled: route.tls_upstream?.enabled ?? false,
    tls_verify: route.tls_upstream?.verify ?? true,
    websocket: route.websocket,
    auth: route.auth?.type ?? "none",
    request_headers: headerRows(route.request_headers),
    response_headers: headerRows(route.response_headers),
    path_prefix: route.path_prefix,
    strip_prefix: route.strip_prefix,
  };
}

function hostsToDraft(route: Exclude<Route, { type: "tcp" }>): Partial<RouteDraft> {
  return {
    hosts: [...route.hosts],
    dns_target: route.dns?.target ?? "haproxy",
    cloudflare_dns: route.cloudflare?.dns ?? false,
    cloudflare_proxied: route.cloudflare?.proxied ?? null,
  };
}

function geoipToDraft(route: Route): Partial<RouteDraft> {
  const countries = route.geoip?.block_countries ?? [];
  // An empty list blocks nothing, so the switch shows off for it whatever the file says.
  return { block_countries: [...countries], geoip_enabled: (route.geoip?.enabled ?? true) && countries.length > 0 };
}

/** The form state of a saved route. */
export function routeToDraft(route: Route): RouteDraft {
  return {
    ...blankDraft(),
    type: route.type, exposure: route.exposure, enabled: route.enabled,
    ...geoipToDraft(route),
    upstreams: route.type === "redirect" ? [] : route.upstreams.map((upstream) => upstream.address),
    ...(route.type === "tcp" ? { listen_port: route.listen_port } : hostsToDraft(route)),
    ...(route.type === "http" ? httpToDraft(route) : {}),
    ...(route.type === "redirect"
      ? { redirect_to: route.redirect.to, redirect_code: route.redirect.code, keep_path: route.redirect.keep_path }
      : {}),
  };
}

/** The fields every route type shares, and the name of the route being edited (a new route gets one from the server). */
function common(draft: RouteDraft, original?: Route) {
  const kept = original?.type === draft.type ? original : undefined;
  return {
    ...kept, version: 1 as const, ...(original ? { name: original.name } : {}),
    exposure: draft.exposure, enabled: draft.enabled,
    // An empty list keeps the file's flag, so editing a route without one adds no `enabled` line.
    geoip: { ...kept?.geoip, block_countries: draft.block_countries, ...(draft.block_countries.length ? { enabled: draft.geoip_enabled } : {}) },
  };
}

/** The host settings of an http, passthrough or redirect route. Passthrough has no "zone default" for proxied. */
function hostSettings(draft: RouteDraft) {
  const proxied = draft.type === "passthrough" ? Boolean(draft.cloudflare_proxied) : draft.cloudflare_proxied;
  return { hosts: draft.hosts, dns: { target: draft.dns_target }, cloudflare: { dns: draft.cloudflare_dns, proxied } };
}

function httpRoute(draft: RouteDraft, original?: Route): Route {
  const kept = original?.type === "http" ? original : undefined;
  return {
    ...common(draft, original), ...hostSettings(draft), type: "http",
    upstreams: draft.upstreams.map((address) => ({ address })),
    health_check: draft.health_path
      ? { path: draft.health_path, interval: kept?.health_check?.interval ?? "10s" }
      : null,
    tls_upstream: { enabled: draft.tls_enabled, verify: draft.tls_verify },
    websocket: draft.websocket,
    auth: { type: draft.auth },
    request_headers: headerRecord(draft.request_headers),
    response_headers: headerRecord(draft.response_headers),
    path_prefix: draft.path_prefix || "/",
    strip_prefix: draft.strip_prefix,
  } as Route;
}

/**
 * Build the route to save from the draft and the route being edited, if any. Fields of other types are
 * dropped, fields the editor lacks are kept, and a new route has no name, so the server names it.
 */
export function draftToRoute(draft: RouteDraft, original?: Route): Route {
  if (draft.type === "http") return httpRoute(draft, original);
  if (draft.type === "redirect") {
    return {
      ...common(draft, original), ...hostSettings(draft), type: "redirect",
      redirect: { to: draft.redirect_to, code: draft.redirect_code, keep_path: draft.keep_path },
    } as Route;
  }
  const upstreams = draft.upstreams.map((address) => ({ address }));
  if (draft.type === "tcp") return { ...common(draft, original), type: "tcp", listen_port: draft.listen_port ?? 0, upstreams } as Route;
  return { ...common(draft, original), ...hostSettings(draft), type: "passthrough", upstreams } as Route;
}

/**
 * Read an upstream as the admin typed or pasted it. A URL gives its host and port (80 or 443 when it
 * has none) and whether it speaks TLS; anything else is kept as typed for the server to check.
 */
export function parseUpstream(value: string): { address: string; tls?: boolean } {
  const match = /^(https?):\/\/([^/?#]+)/i.exec(value.trim());
  if (!match) return { address: value.trim() };
  const tls = match[1]?.toLowerCase() === "https";
  const host = match[2] ?? "";
  return { address: /:\d+$/.test(host) ? host : `${host}:${tls ? 443 : 80}`, tls };
}
