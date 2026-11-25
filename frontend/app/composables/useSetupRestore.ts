/** Restore from a backup in place of setup: the raw archive goes to the API with the setup token. */
export function useSetupRestore(token: Readonly<Ref<string>>) {
  const api = useApi();
  const file = ref<File | null>(null);
  const error = ref("");
  const busy = ref(false);

  /** Send the archive. Returns true once the backup is restored. */
  async function submit(): Promise<boolean> {
    error.value = !token.value ? "Enter the setup token first." : !file.value ? "Choose the backup file." : "";
    if (error.value) return false;
    busy.value = true;
    const answer = await api.POST("/api/v1/setup/restore", {
      params: { header: { "X-Setup-Token": token.value } },
      headers: { "Content-Type": "application/gzip" },
      // The endpoint reads the raw request stream, so the schema declares no body.
      body: file.value as never,
      bodySerializer: (body) => body,
    }).catch(() => undefined);
    busy.value = false;
    if (answer?.response.ok) return true;
    error.value = answer
      ? apiErrorMessage(answer.error, `The backup was not restored (status ${answer.response.status}).`)
      : "The dashboard did not answer. Check the connection and try again.";
    return false;
  }

  return { file, error, busy, submit };
}
