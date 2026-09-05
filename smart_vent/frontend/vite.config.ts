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
      // Ratcheted to just below the measured coverage so the suite can never
      // silently drift back. Measured 99.95 / 100 / 99.20 / 99.57 over two
      // consecutive full runs (identical both times).
      // Every remaining gap is enumerated in the PR that set these; they are
      // defensive fallbacks and unreachable-in-jsdom paths, not untested
      // behaviour. Raise these when coverage rises; never lower them.
      thresholds: {
        lines: 99.9,
        functions: 99.9,
        branches: 99.1,
        statements: 99.5,
      },
    },
  },
});
