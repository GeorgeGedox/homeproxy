const STARTING_SECONDS = 20;

/** The CrowdSec bouncer as `GET /status` reports it, refreshed while the page is open.
 * "starting" means it is down within a few seconds of the newest apply, when it is expected to be. */
export function useBouncerState() {
  // The shared status, so the sidebar's HAProxy light follows these refreshes too.
  const { data, refresh } = useStatus();
  const state = computed(() => {
    const applied = data.value?.last_apply ? Date.now() - new Date(data.value.last_apply.time).getTime() : Infinity;
    return data.value?.bouncer === "down" && applied < STARTING_SECONDS * 1000 ? "starting" : (data.value?.bouncer ?? null);
  });

  const timer = setInterval(() => void (document.hidden || refresh()), 5000);
  onScopeDispose(() => clearInterval(timer));

  return { state, known: computed(() => data.value !== undefined) };
}
