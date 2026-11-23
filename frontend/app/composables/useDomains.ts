import type { components } from "~~/api/schema";
import type { LedTone } from "~/utils/health";
import type { Job } from "~/utils/jobs";

export type Domain = components["schemas"]["DomainView"];

/** One certificate the config needs, installed or not, in the words of its status light. */
export interface CertificateRow {
  name: string;
  names: string[];
  tone: LedTone;
  text: string;
  /** Who issued the installed certificate, such as Let's Encrypt or its staging CA. */
  issuer?: string;
  /** The exact expiry, for a tooltip. */
  expires?: string;
  installed: boolean;
  /** A job is issuing it now, so it offers no action. */
  issuing: boolean;
}

/** A domain as the list shows it: its HTTPS state, its certificates and the sites on it. */
export interface DomainEntry {
  domain: Domain;
  tone: LedTone;
  text: string;
  certificates: CertificateRow[];
  sites: number;
  staged: boolean;
}

const ORDER: LedTone[] = ["down", "unknown", "staged", "up", "off"];

const EXPIRY_TONES: Record<Certificate["state"], LedTone> = { ok: "up", warning: "unknown", alert: "down" };

/** Why the last job failed, or "" when it did not. */
const failureOf = (job?: Job) => (job?.state === "failed" ? (job.error ?? "the issue failed").replace(/\.$/, "") : "");

/** The certificate's own state, unless a newer job is issuing it or failed to. */
function certificateState(cert?: Certificate, job?: Job): { tone: LedTone; text: string } {
  if (job?.state === "queued" || job?.state === "running") return { tone: "unknown", text: "Issuing…" };
  const failed = failureOf(job);
  if (!cert) return failed ? { tone: "down", text: `Failed: ${failed}` } : { tone: "unknown", text: "Not issued yet" };
  const left = daysLeft(cert.days_left);
  if (failed) return { tone: "down", text: `Renewal failed: ${failed}. The current one has ${left}` };
  return { tone: EXPIRY_TONES[cert.state], text: left };
}

/** The Domains page: each domain with its certificates, issue jobs and sites, and its actions. */
export function useDomains() {
  const api = useApi();
  const pending = usePending();
  const certs = useCertificates();
  // Two errors, since the reload after a refused remove would clear its error.
  const loadError = ref("");
  const removeError = ref("");
  const credentialsError = ref("");
  const { data, refresh } = useAsyncData("domains", async () => {
    const [domains, settings, routes, credentials, planned] = await Promise.all([
      api.GET("/api/v1/domains"), api.GET("/api/v1/settings"), api.GET("/api/v1/routes"), api.GET("/api/v1/credentials"),
      api.GET("/api/v1/certificates/planned"),
    ]);
    loadError.value = domains.data ? "" : apiErrorMessage(domains.error, "The domains could not be loaded.");
    credentialsError.value = credentials.data ? "" : apiErrorMessage(credentials.error, "The stored credentials could not be loaded.");
    return {
      domains: domains.data ?? [],
      cloudflareOn: settings.data?.cloudflare.enabled ?? false,
      routes: routes.data ?? [],
      credentials: credentials.data ?? [],
      planned: planned.data ?? [],
    };
  });
  watch([pending.applies, pending.discards], () => void refresh());

  const zones = computed(() => (data.value?.domains ?? []).map((domain) => domain.zone));
  const sitesByZone = computed(() => new Map(groupByZone(groupSites(data.value?.routes ?? []), zones.value).map((group) => [group.name, group.sites.length])));
  const domainChanges = computed(() => pending.summary.value.filter((change) => change.kind === "domain"));
  const stagedZones = computed(() => new Set(domainChanges.value.map((change) => change.label)));
  /** Domains that are not live yet: their certificates come with the apply. */
  const addedZones = computed(() => new Set(domainChanges.value.filter((change) => change.action === "added").map((change) => change.label)));

  /** The latest job for each certificate name: sorted oldest first, so the newest one stays in the map. */
  const latestJobs = computed(() => {
    const sorted = [...certs.jobs.value].sort((a, b) => a.created_at.localeCompare(b.created_at));
    return new Map(sorted.filter((job) => job.subject).map((job) => [job.subject as string, job]));
  });

  /** The certificates the domain needs, as the next apply plans them. */
  const plannedNames = (domain: Domain) => (data.value?.planned ?? []).filter((cert) => cert.zone === domain.zone).map((cert) => cert.name);

  function certificateRows(domain: Domain): CertificateRow[] {
    const installed = certs.certificates.value.filter((cert) => zoneOf(cert.name, zones.value) === domain.zone);
    const known = new Set([...installed.map((cert) => cert.name), ...plannedNames(domain), ...[...latestJobs.value.keys()].filter((name) => zoneOf(name, zones.value) === domain.zone)]);
    return [...known].sort().map((name) => {
      const cert = installed.find((item) => item.name === name);
      const job = latestJobs.value.get(name);
      return { name, names: cert?.names ?? [name], issuer: cert?.issuer, expires: cert ? new Date(cert.not_after).toLocaleDateString() : undefined, installed: Boolean(cert), issuing: job?.state === "queued" || job?.state === "running", ...certificateState(cert, job) };
    });
  }

  function entry(domain: Domain): DomainEntry {
    const added = addedZones.value.has(domain.zone);
    const certificates = domain.dns_provider && !added ? certificateRows(domain) : [];
    const staged = stagedZones.value.has(domain.zone);
    const base = { domain, certificates, sites: sitesByZone.value.get(domain.zone) ?? 0, staged };
    if (!domain.dns_provider) return { ...base, tone: "off", text: "Plain HTTP, no certificate" };
    const worst = ORDER.map((tone) => certificates.find((row) => row.tone === tone)).find(Boolean);
    if (worst) return { ...base, tone: worst.tone, text: worst.text };
    if (added) return { ...base, tone: "staged", text: "Issued after the next apply" };
    // Per-host certificates follow the sites, so a domain with none needs none yet.
    return { ...base, tone: "off", text: "No certificate needed until a site uses it" };
  }

  const entries = computed(() => (data.value?.domains ?? []).map(entry));

  async function remove(domain: Domain): Promise<void> {
    const { response, error: failure } = await api.DELETE("/api/v1/domains/{zone}", { params: { path: { zone: domain.zone } } });
    removeError.value = response.ok ? "" : apiErrorMessage(failure, "The domain was not removed.");
    await Promise.all([refresh(), pending.refresh()]);
  }

  return {
    entries, error: computed(() => loadError.value || removeError.value || certs.error.value), loaded: computed(() => Boolean(data.value)),
    cloudflareOn: computed(() => data.value?.cloudflareOn ?? false), renew: certs.renew, remove,
    credentials: computed(() => data.value?.credentials ?? []), credentialsError,
    reload: () => Promise.all([refresh(), pending.refresh()]),
  };
}
