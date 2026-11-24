// SPDX-License-Identifier: AGPL-3.0-or-later
import js from "@eslint/js";
import tseslint from "typescript-eslint";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";
import prettierConfig from "eslint-config-prettier";

export default tseslint.config(
  { ignores: ["dist"] },
  {
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    files: ["**/*.{ts,tsx}"],
    plugins: {
      "react-hooks": reactHooks,
      "react-refresh": reactRefresh,
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      "react-refresh/only-export-components": ["warn", { allowConstantExport: true }],
      "@typescript-eslint/no-unused-vars": ["error", { argsIgnorePattern: "^_" }],
      "@typescript-eslint/no-explicit-any": "error",
      "no-console": ["warn", { allow: ["warn", "error"] }],
    },
  },
  // Enforce EntityPicker usage: ban direct getHAEntities imports outside the component itself.
  // Tests use `vi.mocked(api.getHAEntities)` via namespace import, so they are unaffected.
  {
    files: ["**/*.{ts,tsx}"],
    ignores: ["**/EntityPicker.tsx"],
    rules: {
      "no-restricted-syntax": [
        "error",
        {
          selector: "ImportSpecifier[imported.name='getHAEntities']",
          message:
            "Use the EntityPicker component instead of calling getHAEntities directly. " +
            "See src/components/EntityPicker.tsx.",
        },
      ],
    },
  },
  // House time zone contract (Issue #729): every time the UI shows or accepts
  // goes through src/time.ts (via useTimeZone / <Time>), so it is in the
  // HOUSE's zone, never the browser's. Formatting a Date directly here would
  // quietly bring the browser's zone back. A later block replaces the rule for
  // the files it matches, so the getHAEntities selector is repeated.
  {
    files: ["**/*.{ts,tsx}"],
    ignores: [
      "**/EntityPicker.tsx",
      "src/time.ts",
      "src/components/Time.tsx",
      "**/*.test.{ts,tsx}",
      "src/test/**",
    ],
    rules: {
      "no-restricted-syntax": [
        "error",
        {
          selector: "ImportSpecifier[imported.name='getHAEntities']",
          message:
            "Use the EntityPicker component instead of calling getHAEntities directly. " +
            "See src/components/EntityPicker.tsx.",
        },
        {
          selector:
            "CallExpression[callee.property.name=/^(toLocaleString|toLocaleDateString|toLocaleTimeString|toISOString)$/]",
          message:
            "Format or parse times with useTimeZone() / <Time> from src/time.ts so they are in " +
            "the house's time zone, not the browser's (Issue #729, docs/time-zones.md).",
        },
      ],
    },
  },
  prettierConfig,
);
