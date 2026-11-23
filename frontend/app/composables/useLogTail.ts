/** One access line as the stream sends it. Its shape is not in the OpenAPI schema, so the fields shown are typed here. */
export interface LogLine {
  kind: "http" | "tcp";
  logged_at: string;
  client_ip: string;
  frontend: string;
  status: number | null;
  request: string | null;
  sni: string | null;
  route: string | null;
  country: string | null;
  line: string;
}

const MAX_LINES = 500;

const key = (line: LogLine) => line.line + line.logged_at;

/** The live access log: filters, Pause and Resume, and why the API refused the filters. */
export function useLogTail() {
  const filters = reactive({ route: "", status: "", client: "", country: "" });
  const lines = ref<LogLine[]>([]);
  /** Lines that arrived while paused, newest first. */
  const held = ref<LogLine[]>([]);
  const paused = ref(false);
  const connected = ref(false);
  const refused = ref("");
  let source: EventSource | undefined;

  const query = () => new URLSearchParams(Object.entries(filters).filter(([, value]) => value.trim()).map(([key, value]) => [key, value.trim()]));

  /** Open the stream for the current filters. A new filter shows its own lines, so it also ends a pause. */
  function connect(): void {
    source?.close();
    refused.value = "";
    paused.value = false;
    const params = query();
    source = new EventSource(`/api/v1/logs/stream?${params}`);
    // The stream has no event ids: a reconnect sends the kept lines again. While paused the list stays, and Resume skips the repeats.
    source.onopen = () => {
      connected.value = true;
      held.value = [];
      if (!paused.value) lines.value = [];
    };
    source.onerror = () => {
      connected.value = false;
      // A closed source will not retry: the API refused the filters.
      if (source?.readyState === EventSource.CLOSED) void explainRefusal(params);
    };
    source.onmessage = (event) => {
      const line = JSON.parse(event.data) as LogLine;
      if (paused.value) held.value = [line, ...held.value].slice(0, MAX_LINES);
      else lines.value = [line, ...lines.value].slice(0, MAX_LINES);
    };
  }

  /** EventSource hides why it was refused, so ask once more to read the API's answer. */
  async function explainRefusal(params: URLSearchParams): Promise<void> {
    const answer = await fetch(`/api/v1/logs/stream?${params}`).catch(() => undefined);
    if (answer?.ok) void answer.body?.cancel();
    const body = answer && !answer.ok ? await answer.json().catch(() => undefined) : undefined;
    // A 422 names each bad filter, such as "status: must be a whole number". Anything else is not about the filters.
    const message = apiErrorMessage(body, answer?.status === 422 ? "Check the filters." : "The dashboard did not answer.");
    const text = `${message.charAt(0).toUpperCase()}${message.slice(1)}`;
    refused.value = answer?.status === 422 ? text : `The log stream stopped: ${text} Reload the page to try again.`;
    // The lines on screen matched the old filters, so they would mislead.
    lines.value = [];
    held.value = [];
  }

  function resume(): void {
    const shown = new Set(lines.value.map(key));
    lines.value = [...held.value.filter((line) => !shown.has(key(line))), ...lines.value].slice(0, MAX_LINES);
    held.value = [];
    paused.value = false;
  }
  const clear = () => Object.assign(filters, { route: "", status: "", client: "", country: "" });

  // Text filters change on Enter or when the field is left, so a half-typed value never opens a stream.
  // The watcher runs once per tick, so the four fields a Clear empties open one stream.
  watch(filters, () => connect());
  onMounted(connect);
  onBeforeUnmount(() => source?.close());

  return { filters, lines, held, paused, connected, refused, resume, clear, key };
}
