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
      // Ratcheted 2026-07 to just below the measured coverage (90.96 /
      // 86.98 / 75.52 / 88.56) so the suite can never silently drift back.
      thresholds: {
        lines: 90.9,
        functions: 86.9,
        branches: 75.5,
        statements: 88.5,
      },
    },
  },
});
