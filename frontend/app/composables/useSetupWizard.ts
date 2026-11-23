import type { components } from "~~/api/schema";

type Network = components["schemas"]["SetupNetwork"];

/** What each undetectable value is for, in plain words. */
export const needLabels: Record<string, { label: string; help: string }> = {
  "network.lan_cidrs": {
    label: "LAN ranges",
    help: "The address range of your LAN, for example 192.168.1.0/24. Separate several ranges with commas. Only these can open the dashboard.",
  },
  "network.haproxy_lan_ip": {
    label: "Proxy LAN IP",
    help: "The LAN address of the machine running HomeProxy, for example 192.168.1.10. Your domains point here.",
  },
  "dns_server.listen": {
    label: "DNS server address",
    help: "The address and port of the built-in DNS server, for example 192.168.1.10:5335.",
  },
  "network.http_port": {
    label: "HTTP port",
    help: "Another program on this machine already listens on port 80, for example your old proxy. Pick a free port, such as 8081.",
  },
  "network.https_port": {
    label: "HTTPS port",
    help: "Another program on this machine already listens on port 443, for example your old proxy. Pick a free port, such as 8444.",
  },
};

/** The first-run wizard: the status from the API, the form, and the submit that logs in. */
export function useSetupWizard() {
  const api = useApi();
  const { data: status } = useAsyncData("setup-status", async () => (await api.GET("/api/v1/setup/status")).data);
  const form = reactive({ token: "", password: "", confirm: "" });
  const needed = reactive<Record<string, string>>({});
  const error = ref("");
  const busy = ref(false);

  const needs = computed(() => status.value?.needs ?? []);
  const detected = computed(() => status.value?.detected);
  const mismatch = computed(() => form.confirm !== "" && form.confirm !== form.password);

  /** The values the API could not detect, at the request paths it listed. */
  function neededBody() {
    const split = (text: string) => text.split(",").map((s) => s.trim()).filter(Boolean);
    const has = (path: string) => needs.value.includes(path);
    const body: { network?: Network; dns_server?: { listen: string } } = {};
    if (has("dns_server.listen")) body.dns_server = { listen: needed["dns_server.listen"] ?? "" };
    const network: Partial<Network> = {};
    if (has("network.lan_cidrs")) network.lan_cidrs = split(needed["network.lan_cidrs"] ?? "");
    if (has("network.haproxy_lan_ip")) network.haproxy_lan_ip = needed["network.haproxy_lan_ip"];
    if (has("network.http_port")) network.http_port = Number(needed["network.http_port"]);
    if (has("network.https_port")) network.https_port = Number(needed["network.https_port"]);
    if (Object.keys(network).length) body.network = network as Network;
    return body;
  }

  async function submit(): Promise<void> {
    if (form.confirm !== form.password) return;
    busy.value = true;
    error.value = "";
    const { response, error: failure } = await api.POST("/api/v1/setup", {
      body: { token: form.token, password: form.password, ...neededBody() },
    });
    if (!response.ok) {
      busy.value = false;
      error.value = apiErrorMessage(failure, "Setup failed.");
      return;
    }
    const login = await api.POST("/api/v1/auth/login", { body: { password: form.password } });
    form.password = "";
    form.confirm = "";
    busy.value = false;
    await navigateTo(login.response.ok ? "/" : "/login");
  }

  return { form, needed, needs, detected, mismatch, error, busy, submit };
}
