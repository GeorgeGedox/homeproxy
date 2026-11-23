interface NavItem {
  label: string;
  to: string;
  /** Literal `lucide:*` name, so the build bundles the icon locally. */
  icon: string;
}

interface NavGroup {
  /** Shown above the group; the first group has none. */
  label?: string;
  items: readonly NavItem[];
}

/** Left navigation, grouped by what the admin is doing. Domains come before Routes: a route needs a domain. */
export const NAV_GROUPS: readonly NavGroup[] = [
  { items: [{ label: "Overview", icon: "lucide:layout-dashboard", to: "/" }] },
  {
    label: "Proxy",
    items: [
      { label: "Domains", icon: "lucide:globe", to: "/domains" },
      { label: "Routes", icon: "lucide:route", to: "/routes" },
    ],
  },
  {
    label: "Network",
    items: [
      { label: "DNS", icon: "lucide:network", to: "/dns" },
      { label: "Cloudflare", icon: "lucide:cloud", to: "/cloudflare" },
    ],
  },
  { label: "Protection", items: [{ label: "Security", icon: "lucide:shield-alert", to: "/security" }] },
  {
    label: "Monitor",
    items: [
      { label: "Metrics", icon: "lucide:chart-column", to: "/metrics" },
      { label: "Logs", icon: "lucide:scroll-text", to: "/logs" },
    ],
  },
  {
    label: "System",
    items: [
      { label: "History", icon: "lucide:history", to: "/history" },
      { label: "Settings", icon: "lucide:settings", to: "/settings" },
    ],
  },
];

/** Whether `item` is the active entry for `path`. */
export function isActive(item: NavItem, path: string): boolean {
  return item.to === "/" ? path === "/" : path === item.to || path.startsWith(`${item.to}/`);
}

/** The staged Settings sections, each a tab of /settings opened by `#id`; `key` is its field in the settings API. */
export const SETTINGS_SECTIONS = [
  { label: "Network", id: "network", key: "network" }, { label: "DNS server", id: "dns", key: "dns_server" },
  { label: "Certificates", id: "certificates", key: "certificates" }, { label: "Cloudflare", id: "cloudflare", key: "cloudflare" },
  { label: "CrowdSec", id: "crowdsec", key: "crowdsec" }, { label: "GeoIP", id: "geoip", key: "geoip" },
  { label: "Notifications", id: "notifications", key: "notifications" }, { label: "Prometheus", id: "prometheus", key: "prometheus" },
] as const;
