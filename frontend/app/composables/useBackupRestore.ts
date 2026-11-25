import type { components } from "~~/api/schema";

type BackupCheck = components["schemas"]["BackupCheck"];

const SESSION_PATHS = { check: "/api/v1/backup/check", restore: "/api/v1/backup/restore" } as const;
const SETUP_PATHS = { check: "/api/v1/setup/restore/check", restore: "/api/v1/setup/restore" } as const;
const NO_ANSWER = "The dashboard did not answer. Check the connection and try again.";

/**
 * Check a backup file as soon as it is chosen, then restore it. Without `setupToken` it restores
 * over this install with the session; with it, in place of setup.
 */
export function useBackupRestore(setupToken?: Readonly<Ref<string>>) {
  const api = useApi();
  const paths = setupToken ? SETUP_PATHS : SESSION_PATHS;
  const file = ref<File | null>(null);
  const summary = ref<BackupCheck>();
  const error = ref("");
  const busy = ref(false);

  async function send(path: (typeof paths)[keyof typeof paths]) {
    busy.value = true;
    // The four routes take the same raw body. The setup token goes in as a plain header, so one typing serves them all.
    const answer = await api.POST(path as typeof SESSION_PATHS.check, {
      headers: { "Content-Type": "application/gzip", ...(setupToken && { "X-Setup-Token": setupToken.value }) },
      // The schema types a binary body as string, and the serializer sends the File as it is.
      body: file.value as unknown as string,
      bodySerializer: (body) => body,
    }).catch(() => undefined);
    busy.value = false;
    if (!answer) error.value = NO_ANSWER;
    return answer;
  }

  /** Check the chosen file. The setup check does not use the setup token up. */
  async function check(): Promise<void> {
    summary.value = undefined;
    error.value = setupToken && !setupToken.value ? "Enter the setup token first." : !file.value ? "Choose the backup file." : "";
    if (error.value) return;
    const answer = await send(paths.check);
    if (!answer) return;
    if (answer.data) summary.value = answer.data;
    else error.value = apiErrorMessage(answer.error, `The backup could not be checked (status ${answer.response.status}).`);
  }

  /** Restore the checked file. Returns true once the backup is restored and the dashboard restarts. */
  async function restore(): Promise<boolean> {
    error.value = "";
    const answer = await send(paths.restore);
    if (!answer) return false;
    if (answer.response.ok) return true;
    error.value = apiErrorMessage(answer.error, `The backup was not restored (status ${answer.response.status}).`);
    return false;
  }

  watch(file, (chosen) => {
    summary.value = undefined;
    error.value = "";
    if (chosen) void check();
  });

  return { file, summary, error, busy, check, restore };
}
