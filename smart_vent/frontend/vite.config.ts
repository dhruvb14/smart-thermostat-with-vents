/// <reference types="vitest" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  base: "./",
  server: {
    proxy: {
      "/api": "http://localhost:8099",
      "/ws": { target: "ws://localhost:8099", ws: true },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
  },
  test: {
    globals: true,
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    coverage: {
      provider: "v8",
      reporter: ["text", "json", "html"],
      exclude: ["node_modules/", "dist/", "src/test/setup.ts"],
      // Thresholds calibrated for Vitest 4's v8 AST-aware coverage remapping,
      // which is more accurate (and reports lower) than the v3 v8-to-istanbul
      // remapping these were originally tuned against.
      thresholds: {
        lines: 90,
        functions: 85,
        branches: 72,
        statements: 87,
      },
    },
  },
});
