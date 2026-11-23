import type { components } from "~~/api/schema";

type Keys = components["schemas"]["KeysSet"];
type KeysInput = components["schemas"]["KeysInput"];

/** Which write-only keys have a value. Values are never returned, so there is nothing else to hold. */
export function useSettingsKeys() {
  const keys = useState<Keys | undefined>("settings-keys", () => undefined);
  const api = useApi();

  async function load(): Promise<void> {
    keys.value = (await api.GET("/api/v1/settings/keys")).data;
  }

  /** Set, replace or (with an empty value) clear one key. Returns an error message, or "" on success. */
  async function put(section: keyof Keys, name: string, value: string): Promise<string> {
    const body = { [section]: { [name]: value } } as KeysInput;
    const { response, error } = await api.PUT("/api/v1/settings/keys", { body });
    if (!response.ok) return apiFieldError(error, "The key was not saved.").message;
    await load();
    return "";
  }

  const isSet = (section: keyof Keys, name: string): boolean | undefined =>
    (keys.value?.[section] as Record<string, boolean> | undefined)?.[name];

  return { load, put, isSet };
}
