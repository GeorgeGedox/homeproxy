import { describe, expect, it } from "vitest";
import { blankDomainForm, credentialUpdate, domainBody, domainFormOf, freeName, type StoredCredential } from "~/utils/domain";

const cloudflareDomain = {
  zone: "example.com", dns_provider: "cloudflare", credentials: "cf-main", wildcard_cert: true,
  cloudflare: { ddns_record: "home.example.com", ddns_update: true, default_proxied: false },
};
const stored: StoredCredential[] = [
  { name: "cf-main", provider: "cloudflare", variables: { api_token: null }, zones: ["example.com", "example.net"] },
  { name: "r53", provider: "route53", variables: { AWS_ACCESS_KEY_ID: null, AWS_SECRET_ACCESS_KEY: null, AWS_REGION: "eu-west-1" }, zones: ["example.org"] },
];

describe("the domain form", () => {
  it("edits the DDNS record as the name under the zone, and an empty one as none", () => {
    const form = domainFormOf(cloudflareDomain);
    expect(form.ddnsName).toBe("home");
    expect(domainBody(form)).toMatchObject({ credentials: "cf-main", cloudflare: { ddns_record: "home.example.com", default_proxied: false } });
    expect(domainBody(form)).not.toHaveProperty("api_token");
    expect(domainBody({ ...form, ddnsName: "" }).cloudflare?.ddns_record).toBeNull();
  });

  it("names new credentials so they never replace stored ones", () => {
    expect(freeName("cloudflare", stored)).toBe("cloudflare");
    expect(freeName("cloudflare", [...stored, { ...stored[0], name: "cloudflare" } as StoredCredential])).toBe("cloudflare-2");
    const form = { ...domainFormOf(cloudflareDomain), stored: "", credentials: "cloudflare", token: "a-new-token" };
    expect(domainBody(form)).toMatchObject({ credentials: "cloudflare", api_token: "a-new-token" });
  });

  it("replaces stored keys: an untouched secret as null, a cleared plain value left out", () => {
    const form = domainFormOf({ zone: "example.org", dns_provider: "route53", credentials: "r53", wildcard_cert: true });
    expect(credentialUpdate(form, stored[1])).toBeUndefined();
    const replacing = { ...form, replacing: true, env: { AWS_SECRET_ACCESS_KEY: "a-new-secret", AWS_REGION: "eu-west-1" } };
    expect(credentialUpdate(replacing, stored[1])).toEqual({ env: { AWS_ACCESS_KEY_ID: null, AWS_SECRET_ACCESS_KEY: "a-new-secret", AWS_REGION: "eu-west-1" } });
    // A plain value cleared in the form is left out, which removes it.
    expect(credentialUpdate({ ...replacing, env: { AWS_REGION: "" } }, stored[1])).toEqual({ env: { AWS_ACCESS_KEY_ID: null, AWS_SECRET_ACCESS_KEY: null } });
  });

  it("sends no keys for new credentials left empty, so the server names what is missing", () => {
    const form = { ...blankDomainForm(), zone: "example.org", https: "other" as const, provider: "route53", credentials: "route53" };
    expect(domainBody(form)).toMatchObject({ dns_provider: "route53", credentials: "route53", env: null });
  });
});
