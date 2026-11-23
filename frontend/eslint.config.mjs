import js from "@eslint/js";
import tseslint from "typescript-eslint";
import pluginVue from "eslint-plugin-vue";

export default tseslint.config(
  {
    ignores: ["node_modules/", ".nuxt/", ".output/", "dist/", "api/", "playwright-report/", "test-results/"],
  },
  js.configs.recommended,
  ...tseslint.configs.strict,
  ...pluginVue.configs["flat/essential"],
  {
    files: ["**/*.vue"],
    languageOptions: { parserOptions: { parser: tseslint.parser } },
  },
  {
    // Nuxt auto-imports are typed by the generated .nuxt types, not by ESLint.
    files: ["**/*.{ts,vue}"],
    rules: { "no-undef": "off", "vue/multi-word-component-names": "off", complexity: ["error", 10] },
  },
  {
    // AGENTS.md "Code style": split a file by responsibility before it reaches 500 lines.
    files: ["app/**/*.{ts,vue}"],
    rules: { "max-lines": ["error", { max: 500, skipBlankLines: true, skipComments: true }] },
  },
);
