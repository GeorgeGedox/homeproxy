import type { RouteLocationNormalized } from "vue-router";

/** Ask before a page with unsaved edits is left: a link or Back opens `open`, closing the tab asks the browser. */
export function useUnsavedGuard(dirty: Readonly<Ref<boolean>>) {
  const open = ref(false);
  let target: RouteLocationNormalized | undefined;
  let discarded = false;

  onBeforeRouteLeave((to) => {
    if (discarded || !dirty.value) return true;
    target = to;
    open.value = true;
    return false;
  });

  const onUnload = (event: BeforeUnloadEvent) => {
    if (dirty.value) event.preventDefault();
  };
  onMounted(() => window.addEventListener("beforeunload", onUnload));
  onBeforeUnmount(() => window.removeEventListener("beforeunload", onUnload));

  /** Leave to where the user was going, dropping the edits. */
  async function discard(): Promise<void> {
    discarded = true;
    open.value = false;
    if (target) await navigateTo(target.fullPath);
  }

  return { open, discard };
}
