import type { components } from "~~/api/schema";

type TokenStatus = components["schemas"]["BackupTokenStatus"];

/** The backup token: its state, and the calls that make, replace and remove it. */
export function useBackup() {
  const api = useApi();
  const toast = useToast();
  const status = ref<TokenStatus>();
  /** The new token. It is shown once, so it lives only here until the admin closes it. */
  const token = ref("");
  const error = ref("");
  const busy = ref(false);

  async function load(): Promise<void> {
    const { data, error: failure } = await api.GET("/api/v1/backup/token");
    if (data) status.value = data;
    else error.value = apiErrorMessage(failure, "The backup token state could not be loaded.");
  }

  async function create(): Promise<void> {
    busy.value = true;
    error.value = "";
    const { data, error: failure } = await api.POST("/api/v1/backup/token");
    busy.value = false;
    if (!data) return void (error.value = apiErrorMessage(failure, "The backup token was not made."));
    token.value = data.token;
    await load();
  }

  async function remove(): Promise<void> {
    busy.value = true;
    error.value = "";
    const { response, error: failure } = await api.DELETE("/api/v1/backup/token");
    busy.value = false;
    if (!response.ok) return void (error.value = apiErrorMessage(failure, "The backup token was not removed."));
    token.value = "";
    toast.add({ title: "Backup token removed", description: "A script that holds it gets 401 from now on." });
    await load();
  }

  /** Copy `text` and say what it is: "backup token" or "key pin". */
  async function copy(text: string, what: string): Promise<void> {
    await navigator.clipboard.writeText(text);
    toast.add({ title: "Copied", description: `The ${what} is on your clipboard.` });
  }

  return { status, token, error, busy, load, create, remove, copy };
}
