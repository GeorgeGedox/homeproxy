/** Restore from a backup in place of setup: the raw archive goes to the API with the setup token. */
export function useSetupRestore(token: Readonly<Ref<string>>) {
  const file = ref<File | null>(null);
  const error = ref("");
  const busy = ref(false);
  const restored = ref(false);

  async function submit(): Promise<void> {
    if (!file.value) return void (error.value = "Choose the backup file.");
    busy.value = true;
    error.value = "";
    try {
      const answer = await fetch("/api/v1/setup/restore", {
        method: "POST",
        headers: { "Content-Type": "application/gzip", "X-Setup-Token": token.value },
        body: file.value,
      });
      if (answer.ok) return void (restored.value = true);
      const body: unknown = await answer.json().catch(() => undefined);
      error.value = apiErrorMessage(body, `The backup was not restored (status ${answer.status}).`);
    } catch {
      error.value = "The dashboard did not answer. Check the connection and try again.";
    } finally {
      busy.value = false;
    }
  }

  return { file, error, busy, restored, submit };
}
