import { h } from "vue";

/** A toast for a save that was staged with warnings: the count in the title, one warning per line. */
export function warningToast(warnings: string[]) {
  return {
    title: `Staged with ${warningCount(warnings)}`,
    description: h("ul", { class: "space-y-1.5" }, warnings.map((warning) => h("li", warning))),
    color: "warning" as const,
  };
}

/** "a warning" or "3 warnings". */
export const warningCount = (warnings: readonly string[]) => (warnings.length > 1 ? `${warnings.length} warnings` : "a warning");
