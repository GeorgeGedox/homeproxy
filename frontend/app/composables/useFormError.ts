import type { Form } from "@nuxt/ui";

type FormApi = Pick<Form<object>, "setErrors" | "getErrors" | "clear">;

/**
 * Show an API error at the `UFormField` its `field` names, or in `error` when the form has no such
 * field. `UForm` drops an error whose name no field registered, so `getErrors` tells which it was.
 */
export function useFormError(form: Readonly<Ref<FormApi | null | undefined>>) {
  const error = ref("");

  function show(failure: unknown, fallback: string): void {
    const { field, message } = apiFieldError(failure, fallback);
    form.value?.setErrors(field ? [{ name: field, message }] : []);
    const [placed] = form.value?.getErrors() ?? [];
    error.value = placed ? "" : apiErrorMessage(failure, fallback);
    // The save button can be far below the field, so bring the field into view, unless the user
    // is typing: a live check must not move the cursor.
    if (placed?.id && !(document.activeElement instanceof HTMLInputElement)) reveal(placed.id);
  }

  function clear(): void {
    error.value = "";
    form.value?.clear();
  }

  return { error, show, clear };
}

/** Scroll a field to the middle and focus it. UForm disables its fields while a submit runs, which makes focus() a no-op, so scroll first. */
function reveal(id: string): void {
  const field = document.getElementById(id);
  field?.scrollIntoView({ block: "center" });
  field?.focus({ preventScroll: true });
}
