// Orange marks the one action that matters and anything staged; yellow stays for warnings.
export default defineAppConfig({
  ui: {
    colors: { primary: "orange", neutral: "neutral", success: "emerald", warning: "yellow", error: "red" },
    // Room under every page for the floating pending bar, also when the keyboard scrolls a control into view.
    dashboardPanel: { slots: { body: "pb-24 sm:pb-28 scroll-pb-24 sm:scroll-pb-28" } },
    dashboardNavbar: { slots: { title: "font-medium" } },
    // An on switch is a state, not an action, so it stays off the accent. The track stays 36x20 px,
    // but an invisible margin makes it a 52x44 px tap target on a phone.
    switch: { defaultVariants: { color: "neutral" }, slots: { base: "relative before:absolute before:-inset-x-2 before:-inset-y-3" } },
    accordion: { slots: { trigger: "text-toned transition-colors hover:text-highlighted" } },
    // Nuxt UI hovers a quiet button with the elevated surface, which vanishes on anything already elevated
    // (the pending bar, cards). One step brighter shows on every surface.
    button: {
      compoundVariants: [
        { color: "neutral", variant: "ghost", class: "hover:bg-accented active:bg-accented disabled:bg-transparent" },
        { color: "neutral", variant: "outline", class: "hover:bg-accented active:bg-accented" },
      ],
    },
  },
});
