// Dev only: forward /api to a running backend, for example
// HOMEPROXY_DEV_API=http://127.0.0.1:<port>. Production serves both from one origin.
// changeOrigin stays false: the API checks that Origin matches Host on every
// change, so the proxy must pass the browser's Host through unchanged.
const devApi = process.env.HOMEPROXY_DEV_API;

export default defineNuxtConfig({
  ssr: false,
  compatibilityDate: "2026-11-01",
  modules: ["@nuxt/ui"],
  devtools: { enabled: false },
  telemetry: false,
  css: ["~/assets/css/main.css"],
  // flag-icons references some 500 small SVGs; inlined, they would add about 0.5 MB to every page's CSS.
  vite: { build: { assetsInlineLimit: (file: string) => (file.includes("/flag-icons/") ? false : undefined) } },
  // The CSS loads the few flags a page shows; a prefetch hint for each would fetch all 5 MB on every first visit.
  hooks: {
    "build:manifest": (manifest) => {
      const flags = new Set(Object.entries(manifest).filter(([id]) => id.includes("/flag-icons/")).map(([, entry]) => entry.file));
      for (const entry of Object.values(manifest)) if (flags.has(entry.file)) entry.prefetch = false;
    },
  },
  // Dark theme only. The module's color-mode and font handling are off, because
  // the first would add a toggle and the second fetches web fonts.
  ui: { colorMode: false, fonts: false },
  // Icons come from the local lucide set, never from the Iconify API.
  icon: { mode: "svg", provider: "none", fallbackToApi: false, clientBundle: { scan: { globInclude: ["app/**/*.{vue,ts}"] } } },
  typescript: { strict: true },
  app: {
    head: {
      htmlAttrs: { class: "dark", lang: "en" },
      title: "HomeProxy",
      meta: [{ name: "viewport", content: "width=device-width, initial-scale=1" }],
      link: [{ rel: "icon", type: "image/svg+xml", href: "/favicon.svg" }],
    },
  },
  nitro: devApi ? { devProxy: { "/api": { target: `${devApi}/api`, changeOrigin: false } } } : {},
});
