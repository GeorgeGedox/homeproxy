/** Form state for one domain, and its provider and credential choices. */
export function useDomainForm() {
  const form = reactive(blankDomainForm());
  const providerCode = computed(() => providerOf(form));

  const reset = (domain?: Domain) => Object.assign(form, domain ? domainFormOf(domain) : blankDomainForm());

  /** Pick a provider: use stored credentials of that provider when there are some, or start new ones under a free name. */
  function pickProvider(code: string, stored: StoredCredential[]): void {
    Object.assign(form, { provider: code, credentials: freeName(code, stored), token: "", env: {}, replacing: false });
    form.stored = stored.find((credential) => credential.provider === code)?.name ?? "";
  }

  /** Switch between stored and new credentials. New ones get a free name, so they never replace stored ones. */
  function pickStored(name: string, stored: StoredCredential[]): void {
    Object.assign(form, { stored: name, token: "", env: {}, replacing: false });
    if (!name) form.credentials = freeName(providerCode.value, stored);
  }

  return { form, providerCode, reset, pickProvider, pickStored };
}
