import type { components } from "~~/api/schema";

type UrlsSet = components["schemas"]["NotificationUrlsSet"];
type UrlsInput = components["schemas"]["NotificationUrlsInput"];

/** Every event a target can pick. A new target starts with all of them, like the backend default. */
export const NOTIFICATION_EVENTS: NonNullable<components["schemas"]["NotificationTarget"]["events"]> = [
  "certificate_failed",
  "certificate_expiring",
  "apply_failed",
  "route_down",
  "bouncer_down",
  "appsec_down",
  "ddns_failed",
  "geoip_failed",
  "new_version",
];

/** Which write-only notification URLs have a value. URLs are never returned. */
export function useNotificationUrls() {
  const urls = useState<UrlsSet | undefined>("notification-urls", () => undefined);
  const api = useApi();

  async function load(): Promise<void> {
    urls.value = (await api.GET("/api/v1/notifications/urls")).data;
  }

  /** Set, replace or (with an empty value) clear URLs. Returns an error message, or "" on success. */
  async function put(body: UrlsInput): Promise<string> {
    const { response, error } = await api.PUT("/api/v1/notifications/urls", { body });
    if (!response.ok) return apiFieldError(error, "The URL was not saved.").message;
    await load();
    return "";
  }

  return { urls, load, put };
}
