/** Form state for one domain, its provider and credential choices, and its save. */
export function useDomainForm() {
  const api = useApi();
  const form = reactive(blankDomainForm());
  const providerCode = computed(() => providerOf(form));
  const stored = ref<StoredCredential[]>([]);
  /** Without the stored credentials, a save could drop typed keys or name new ones over stored ones. */
  const loadError = ref("");

  /** Start from `domain`, or a new one, and load the stored credentials. */
  async function load(domain?: Domain): Promise<void> {
    Object.assign(form, domain ? domainFormOf(domain) : blankDomainForm());
    const list = await api.GET("/api/v1/credentials");
    stored.value = list.data ?? [];
    loadError.value = list.data ? "" : apiErrorMessage(list.error, "The stored credentials could not be loaded.");
  }

  /** Pick a provider: use stored credentials of that provider when there are some, or start new ones under a free name. */
  function pickProvider(code: string): void {
    Object.assign(form, { provider: code, credentials: freeName(code, stored.value), token: "", env: {}, replacing: false });
    form.stored = stored.value.find((credential) => credential.provider === code)?.name ?? "";
  }

  /** Switch between stored and new credentials. New ones get a free name, so they never replace stored ones. */
  function pickStored(name: string): void {
    Object.assign(form, { stored: name, token: "", env: {}, replacing: false });
    if (!name) form.credentials = freeName(providerCode.value, stored.value);
  }

  /** Save typed credentials, then stage the domain: an edit of `domain`, or a new one. Returns whether both were saved; `refuse` shows why not. */
  async function save(domain: Domain | undefined, refuse: (failure: unknown, fallback: string) => void): Promise<boolean> {
    const update = credentialUpdate(form, stored.value.find((credential) => credential.name === form.stored));
    if (update) {
      const { response, error } = await api.PUT("/api/v1/credentials/{name}", { params: { path: { name: form.stored } }, body: update });
      if (!response.ok) {
        refuse(error, "The credentials were not saved.");
        return false;
      }
    }
    const { response, error } = domain
      ? await api.PUT("/api/v1/domains/{zone}", { params: { path: { zone: domain.zone } }, body: domainBody(form) })
      : await api.POST("/api/v1/domains", { body: domainBody(form) });
    if (!response.ok) refuse(error, "The domain was not staged.");
    return response.ok;
  }

  return { form, providerCode, stored, loadError, load, pickProvider, pickStored, save };
}
