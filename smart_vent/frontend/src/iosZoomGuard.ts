// ---------------------------------------------------------------------------
// iOS focus-zoom guard (Issue #581)
//
// WebKit on iOS/iPadOS auto-zooms the viewport whenever a form control is
// focused whose computed font-size is under 16px. Every field in Plenum is
// below that line — `.form-control` is 0.875rem (14px) and
// `.form-control-sm` is 0.8rem (12.8px) — so tapping any input in the
// home-screen PWA jerks the whole page in and re-centres it, which is the one
// thing a native app never does.
//
// The usual remedy is to raise every control to 16px. We deliberately do not:
// that reflows every form on every platform (and invalidates the whole golden
// screenshot set) to work around a mobile-only WebKit heuristic. Instead we use
// the other lever WebKit offers — it skips the auto-zoom when the viewport meta
// pins `maximum-scale=1` — and apply it only for the lifetime of the focus, so
// nothing about the page's rendered size or its normal zoom behaviour changes.
//
// Why not just put `maximum-scale=1` in index.html: engines other than WebKit
// really do disable pinch-zoom on it, which would be a WCAG 1.4.4 regression
// for everyone to fix an iOS-only annoyance. (On iOS itself it is safe — since
// iOS 10 Safari honours `maximum-scale` for the *automatic* focus zoom but
// ignores it for user-initiated pinch-zoom, so pinch still works there.)
// ---------------------------------------------------------------------------

/** Below this computed font-size (px), WebKit zooms the viewport on focus. */
const ZOOM_THRESHOLD_PX = 16;

/** Appended to the viewport meta for the duration of a focus. */
const SCALE_LOCK = "maximum-scale=1";

/** Elements that can take focus and show an editing affordance. */
const EDITABLE_SELECTOR =
  'input, select, textarea, [contenteditable]:not([contenteditable="false"])';

// `<input>` types iOS never zooms for, because focusing them opens no keyboard
// and needs no legibility bump. Everything not listed here — including any type
// added to the platform later — is assumed to zoom: locking the scale when we
// did not have to costs nothing, failing to lock when we should have is the bug.
const NON_ZOOMING_INPUT_TYPES = new Set([
  "button",
  "checkbox",
  "color",
  "file",
  "hidden",
  "image",
  "radio",
  "range",
  "reset",
  "submit",
]);

/** The slice of `navigator` the platform check reads. */
export type UserAgentProbe = Pick<Navigator, "userAgent" | "vendor" | "maxTouchPoints">;

/**
 * True only for WebKit on a touch-capable Apple device — the sole engine that
 * performs the focus zoom.
 *
 * The UA string alone is not enough. Playwright's `mobile` project runs real
 * Chromium behind an iPhone user-agent (see e2e/playwright.config.ts), and
 * Chromium never auto-zooms; without a second signal the guard would rewrite
 * the viewport mid-run in the visual-regression suite. `navigator.vendor` is
 * frozen per engine ("Apple Computer, Inc." on every WebKit build, "Google
 * Inc." on Chromium) and, unlike the UA, device emulation does not rewrite it.
 *
 * Chrome/Firefox/Edge on iOS are WebKit underneath and report Apple's vendor,
 * so they are correctly included — they zoom exactly like Safari does.
 */
export function isIosWebKit(nav: UserAgentProbe): boolean {
  const ua = nav.userAgent;
  const appleTouchDevice =
    /\b(iPhone|iPod|iPad)\b/.test(ua) ||
    // iPadOS 13+ reports a desktop Mac UA; multi-touch gives it away.
    (/\bMacintosh\b/.test(ua) && nav.maxTouchPoints > 1);
  return appleTouchDevice && nav.vendor === "Apple Computer, Inc.";
}

/**
 * The control's computed font-size in px, or 0 when it cannot be resolved.
 *
 * A real engine always reports a computed font-size in px, so anything else —
 * an empty string, or an unresolved relative unit — means we are not looking
 * at a laid-out element and cannot tell. That reads as 0, i.e. "assume it
 * zooms", for the same reason as the input-type denylist above: a needless
 * pin is invisible, a missed one is the bug.
 */
function computedFontSizePx(el: Element, view: Window): number {
  const [, px] = /^([\d.]+)px$/.exec(view.getComputedStyle(el).fontSize.trim()) ?? [];
  return px ? Number.parseFloat(px) : 0;
}

/** Whether focusing `target` (or its nearest editable ancestor) would zoom. */
function wouldZoom(target: EventTarget | null, view: Window): boolean {
  const el = (target as Element | null)?.closest?.(EDITABLE_SELECTOR);
  if (!el) return false;
  // `.type` on an input is the normalised type ("text" for a missing or
  // unrecognised attribute), which is what the denylist is written against.
  if (el.tagName === "INPUT" && NON_ZOOMING_INPUT_TYPES.has((el as HTMLInputElement).type)) {
    return false;
  }
  return computedFontSizePx(el, view) < ZOOM_THRESHOLD_PX;
}

const NOOP = () => {};

/**
 * Suppress iOS's focus auto-zoom for the lifetime of each focus.
 *
 * Call once at startup. Returns a disposer that removes the listeners and
 * restores the viewport — used by the tests; production never tears down.
 */
export function installIosZoomGuard({
  doc = document,
  nav = navigator,
}: { doc?: Document; nav?: UserAgentProbe } = {}): () => void {
  if (!isIosWebKit(nav)) return NOOP;

  const view = doc.defaultView;
  const meta = doc.querySelector<HTMLMetaElement>('meta[name="viewport"]');
  if (!view || !meta) return NOOP;

  const unlocked = meta.content;
  // A document that already pins the scale needs no help — and appending a
  // second `maximum-scale` would leave the meta with two conflicting values.
  if (/\bmaximum-scale\s*=/.test(unlocked)) return NOOP;
  const locked = `${unlocked}, ${SCALE_LOCK}`;

  let restoreTimer: ReturnType<typeof setTimeout> | undefined;

  const cancelRestore = () => {
    if (restoreTimer !== undefined) {
      clearTimeout(restoreTimer);
      restoreTimer = undefined;
    }
  };

  const scheduleRestore = () => {
    cancelRestore();
    // Deferred by a task, and re-checked against the element that ended up
    // focused. `focusout` on the old field and `focusin` on the new one fire
    // synchronously during a focus move, so releasing immediately would unpin
    // and repin around the very moment WebKit decides whether to zoom; and
    // consulting `activeElement` once the move has settled keeps the pin held
    // without depending on the order the two events arrive in.
    restoreTimer = setTimeout(() => {
      restoreTimer = undefined;
      if (wouldZoom(doc.activeElement, view)) return;
      meta.content = unlocked;
    }, 0);
  };

  const sync = (event: Event) => {
    if (wouldZoom(event.target, view)) {
      cancelRestore();
      meta.content = locked;
    } else {
      // Not a zooming target. Releasing here (rather than only on focusout)
      // means any tap on the page recovers a pin that was somehow left set —
      // e.g. if a modal unmounts its focused field without the engine firing
      // focusout, which the spec's focus fixup rule requires but which has
      // historically varied between engines.
      scheduleRestore();
    }
  };

  // `touchstart` fires before focus is assigned, so on a tap the viewport is
  // already pinned by the time WebKit evaluates the zoom. `focusin` is the
  // authoritative arm and also covers focus moved without a touch — keyboard
  // Tab, the keyboard accessory bar's next/previous, and programmatic focus().
  doc.addEventListener("touchstart", sync, { capture: true, passive: true });
  doc.addEventListener("focusin", sync);
  doc.addEventListener("focusout", scheduleRestore);

  return () => {
    doc.removeEventListener("touchstart", sync, { capture: true });
    doc.removeEventListener("focusin", sync);
    doc.removeEventListener("focusout", scheduleRestore);
    cancelRestore();
    meta.content = unlocked;
  };
}
