import type { ReactNode } from "react";

// ---------------------------------------------------------------------------
// CI-frozen UI flag — deterministic Playwright visual-regression screenshots.
//
// The E2E workflow patches config.yaml to `version: CI`, which the Docker build
// bakes into VITE_APP_VERSION (see smart_vent/Dockerfile). When that flag is
// set we freeze every volatile UI surface — wall-clock strings, live countdown
// timers, and the engine-driven action/event/cycle feeds — so two screenshots
// taken seconds apart (the workflow's update pass → verify pass) are identical.
//
// In production VITE_APP_VERSION is a real semver, so `isCI` is a compile-time
// `false`. See issue #182.
// ---------------------------------------------------------------------------
export const isCI = import.meta.env.VITE_APP_VERSION === "CI";

// Fixed-width placeholder shown in place of a volatile value under CI. A
// constant keeps the element's width stable run-to-run; a varying width (e.g. a
// "7h 5m" countdown) would shift sibling layout and cause spurious pixel diffs.
export const FROZEN = "—";

/**
 * Render `children` normally, but under CI render `frozen` instead.
 *
 * - inline value:    <Frozen>{clock.toLocaleTimeString()}</Frozen>   → "—"
 * - custom placeholder: <Frozen frozen="(frozen)">{feed}</Frozen>
 * - drop a region:   <Frozen frozen={null}>{progressBar}</Frozen>    → nothing
 *
 * Funnelling every freeze through this one component keeps the single `isCI`
 * branch in one tested place — call sites stay branch-free, so they don't dent
 * frontend branch coverage.
 */
export function Frozen({ children, frozen = FROZEN }: { children: ReactNode; frozen?: ReactNode }) {
  return <>{isCI ? frozen : children}</>;
}
