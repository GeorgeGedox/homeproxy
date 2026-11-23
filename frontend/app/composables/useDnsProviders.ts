import type { components } from "~~/api/schema";

export type DnsProvider = components["schemas"]["LegoProvider"];

/** The DNS providers the backend offers, each with the variables it takes. Loaded once. */
export function useDnsProviders() {
  const providers = useState<DnsProvider[]>("dns-providers", () => []);
  const api = useApi();

  async function load(): Promise<void> {
    if (providers.value.length) return;
    providers.value = (await api.GET("/api/v1/domains/providers")).data ?? [];
  }

  return { providers, load };
}
