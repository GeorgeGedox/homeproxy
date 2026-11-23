import type { components } from "~~/api/schema";

type DomainView = components["schemas"]["DomainView"];
type DomainInput = components["schemas"]["DomainInput"];
export type StoredCredential = components["schemas"]["CredentialView"];
type CredentialInput = components["schemas"]["CredentialInput"];

export const CLOUDFLARE = "cloudflare";
export const SELF_SIGNED = "self-signed";
/** How a domain gets HTTPS. "other" is any DNS provider but Cloudflare. */
export type HttpsKind = "none" | "self-signed" | "cloudflare" | "other";

/** What the domain form edits. */
export interface DomainFormState {
  zone: string;
  https: HttpsKind;
  /** The DNS provider's code for "other". */
  provider: string;
  /** Stored credentials to use, or "" for new ones under `credentials`. */
  stored: string;
  credentials: string;
  token: string;
  env: Record<string, string>;
  /** Whether the stored values are being replaced. */
  replacing: boolean;
  wildcard: boolean;
  ddnsName: string;
  ddnsUpdate: boolean;
  proxied: boolean;
}

export const blankDomainForm = (): DomainFormState => ({
  zone: "", https: "none", provider: "", stored: "", credentials: "", token: "", env: {}, replacing: false,
  wildcard: true, ddnsName: "", ddnsUpdate: true, proxied: true,
});

const httpsOf = (provider?: string | null): HttpsKind =>
  !provider ? "none" : provider === SELF_SIGNED ? "self-signed" : provider === CLOUDFLARE ? "cloudflare" : "other";

/** The Cloudflare fields of a domain, with the DDNS record as the name under the zone. */
function cloudflareOf(domain: DomainView) {
  const suffix = `.${domain.zone}`;
  const ddns = domain.cloudflare?.ddns_record ?? "";
  return {
    ddnsName: ddns.endsWith(suffix) ? ddns.slice(0, -suffix.length) : ddns,
    ddnsUpdate: domain.cloudflare?.ddns_update ?? true,
    proxied: domain.cloudflare?.default_proxied ?? true,
  };
}

/** The form state for editing a stored domain. */
export function domainFormOf(domain: DomainView): DomainFormState {
  return {
    ...blankDomainForm(),
    zone: domain.zone,
    https: httpsOf(domain.dns_provider),
    provider: domain.dns_provider ?? "",
    stored: domain.credentials ?? "",
    credentials: domain.credentials ?? "",
    wildcard: domain.wildcard_cert,
    ...cloudflareOf(domain),
  };
}

/** The provider code the form's HTTPS choice uses, or "" for none and self-signed. */
export const providerOf = (form: DomainFormState) => (form.https === "cloudflare" ? CLOUDFLARE : form.https === "other" ? form.provider : "");

/** The provider's code, or the first of `code-2` to `code-9` that no stored credentials use. */
export function freeName(code: string, stored: StoredCredential[]): string {
  const taken = new Set(stored.map((credential) => credential.name));
  return [code, ...[2, 3, 4, 5, 6, 7, 8, 9].map((n) => `${code}-${n}`)].find((name) => !taken.has(name)) ?? code;
}

const filled = (env: Record<string, string>) => Object.fromEntries(Object.entries(env).filter(([, value]) => value));

/** The body that stages the domain. Stored credentials send no keys; new ones send what was typed. */
export function domainBody(form: DomainFormState): DomainInput {
  const zone = form.zone.trim();
  if (form.https === "none") return { zone, wildcard_cert: false, dns_provider: null, credentials: null };
  if (form.https === "self-signed") return { zone, wildcard_cert: form.wildcard, dns_provider: SELF_SIGNED, credentials: null };
  const provider = providerOf(form);
  const base = { zone, wildcard_cert: form.wildcard, dns_provider: provider, credentials: form.stored || form.credentials };
  const env = filled(form.env);
  const fresh = form.stored ? {} : provider === CLOUDFLARE ? { api_token: form.token || null } : { env: Object.keys(env).length ? env : null };
  if (provider !== CLOUDFLARE) return { ...base, ...fresh };
  const ddns = form.ddnsName.trim().replace(new RegExp(`\\.?${zone.replaceAll(".", "\\.")}$`), "");
  const cloudflare = { ddns_record: ddns ? `${ddns}.${zone}` : null, ddns_update: form.ddnsUpdate, default_proxied: form.proxied };
  return { ...base, ...fresh, cloudflare };
}

/**
 * The new values for stored credentials, or undefined when they stay. A secret left empty keeps its stored
 * value (null). The API removes a variable left out, so a plain value the admin cleared goes.
 */
export function credentialUpdate(form: Pick<DomainFormState, "stored" | "replacing" | "token" | "env">, stored?: StoredCredential): CredentialInput | undefined {
  if (!form.stored || !form.replacing || !stored) return undefined;
  if (stored.provider === CLOUDFLARE) return form.token ? { api_token: form.token } : undefined;
  // A secret's stored value reads as null; a plain one arrives prefilled in `env`.
  const kept = Object.fromEntries(Object.entries(stored.variables).filter(([, value]) => value === null).map(([name]) => [name, null]));
  return { env: { ...kept, ...filled(form.env) } };
}
