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
  prettierConfig,
);
