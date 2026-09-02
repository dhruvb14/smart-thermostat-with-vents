import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { installIosZoomGuard, isIosWebKit, type UserAgentProbe } from "./iosZoomGuard";

const BASE_VIEWPORT = "width=device-width, initial-scale=1.0";
const LOCKED_VIEWPORT = `${BASE_VIEWPORT}, maximum-scale=1`;

const APPLE_VENDOR = "Apple Computer, Inc.";
const IPHONE_UA =
  "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 " +
  "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1";
const MAC_UA =
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 " +
  "(KHTML, like Gecko) Version/18.0 Safari/605.1.15";
const DESKTOP_CHROME_UA =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) " +
  "Chrome/131.0.0.0 Safari/537.36";

const probe = (over: Partial<UserAgentProbe> = {}): UserAgentProbe => ({
  userAgent: IPHONE_UA,
  vendor: APPLE_VENDOR,
  maxTouchPoints: 5,
  ...over,
});

/** The viewport meta the guard mutates. Re-created per test. */
let meta: HTMLMetaElement;
let dispose: () => void;

function addViewportMeta(content = BASE_VIEWPORT) {
  meta = document.createElement("meta");
  meta.name = "viewport";
  meta.content = content;
  document.head.appendChild(meta);
}

/**
 * Append a focusable control. jsdom does not run the CSS cascade, so the
 * font-size is set inline — `.form-control` is 14px and `.form-control-sm`
 * 12.8px in styles.css, both under the 16px threshold.
 */
function addField(tag: "input" | "select" | "textarea" | "div", fontSize = "14px", type?: string) {
  const el = document.createElement(tag);
  if (type && el instanceof HTMLInputElement) el.type = type;
  if (tag === "div") el.setAttribute("contenteditable", "true");
  el.style.fontSize = fontSize;
  document.body.appendChild(el);
  return el;
}

/** Fire the event pair a real focus produces, without needing a layout engine. */
function focus(el: Element) {
  el.dispatchEvent(new FocusEvent("focusin", { bubbles: true }));
}
function blur(el: Element) {
  el.dispatchEvent(new FocusEvent("focusout", { bubbles: true }));
}

beforeEach(() => {
  vi.useFakeTimers();
  dispose = () => {};
  addViewportMeta();
});

afterEach(() => {
  dispose();
  vi.useRealTimers();
  document.head.innerHTML = "";
  document.body.innerHTML = "";
});

describe("isIosWebKit", () => {
  it("accepts Safari on iPhone", () => {
    expect(isIosWebKit(probe())).toBe(true);
  });

  it("accepts iPadOS 13+, which reports a desktop Mac UA plus multi-touch", () => {
    expect(isIosWebKit(probe({ userAgent: MAC_UA, maxTouchPoints: 5 }))).toBe(true);
  });

  it("rejects a real desktop Mac (Apple vendor, but no touch)", () => {
    expect(isIosWebKit(probe({ userAgent: MAC_UA, maxTouchPoints: 0 }))).toBe(false);
  });

  it("rejects desktop Chrome", () => {
    expect(
      isIosWebKit(probe({ userAgent: DESKTOP_CHROME_UA, vendor: "Google Inc.", maxTouchPoints: 0 }))
    ).toBe(false);
  });

  // Regression guard for the visual-regression suite: playwright.config.ts's
  // `mobile` project pairs devices["iPhone 14"] with browserName "chromium",
  // so the UA says iPhone while the engine is Chromium — which never zooms.
  // The guard must stay inert there or it would rewrite the viewport mid-run.
  it("rejects Chromium wearing an iPhone user-agent (Playwright's mobile project)", () => {
    expect(isIosWebKit(probe({ vendor: "Google Inc." }))).toBe(false);
  });
});

describe("installIosZoomGuard", () => {
  it("pins maximum-scale while a 14px input is focused and releases it after", () => {
    dispose = installIosZoomGuard({ nav: probe() });
    const input = addField("input");

    focus(input);
    expect(meta.content).toBe(LOCKED_VIEWPORT);

    blur(input);
    // Still locked until the deferred restore runs.
    expect(meta.content).toBe(LOCKED_VIEWPORT);
    vi.runAllTimers();
    expect(meta.content).toBe(BASE_VIEWPORT);
  });

  it("pins the scale on touchstart, before focus is assigned", () => {
    dispose = installIosZoomGuard({ nav: probe() });
    const input = addField("input");

    input.dispatchEvent(new Event("touchstart", { bubbles: true }));
    expect(meta.content).toBe(LOCKED_VIEWPORT);
  });

  it("stays locked while focus moves from one field to the next", () => {
    dispose = installIosZoomGuard({ nav: probe() });
    const first = addField("input");
    const second = addField("input");

    focus(first);
    // A real focus move fires focusout then focusin in the same task.
    blur(first);
    focus(second);
    vi.runAllTimers();

    expect(meta.content).toBe(LOCKED_VIEWPORT);
  });

  it("covers select, textarea and contenteditable", () => {
    dispose = installIosZoomGuard({ nav: probe() });

    for (const tag of ["select", "textarea", "div"] as const) {
      const el = addField(tag);
      focus(el);
      expect(meta.content).toBe(LOCKED_VIEWPORT);
      blur(el);
      vi.runAllTimers();
      expect(meta.content).toBe(BASE_VIEWPORT);
    }
  });

  it("locks when focus lands on a child of an editable region", () => {
    dispose = installIosZoomGuard({ nav: probe() });
    const region = addField("div");
    const child = document.createElement("span");
    region.appendChild(child);

    focus(child);
    expect(meta.content).toBe(LOCKED_VIEWPORT);
  });

  it("leaves the viewport alone for a field already at 16px", () => {
    dispose = installIosZoomGuard({ nav: probe() });
    const input = addField("input", "16px");

    focus(input);
    expect(meta.content).toBe(BASE_VIEWPORT);
  });

  it("leaves the viewport alone for input types iOS never zooms for", () => {
    dispose = installIosZoomGuard({ nav: probe() });

    for (const type of ["checkbox", "radio", "range", "submit"]) {
      const input = addField("input", "14px", type);
      focus(input);
      expect(meta.content).toBe(BASE_VIEWPORT);
    }
  });

  it("locks for keyboard-opening input types", () => {
    dispose = installIosZoomGuard({ nav: probe() });
    const input = addField("input", "14px", "number");

    focus(input);
    expect(meta.content).toBe(LOCKED_VIEWPORT);
  });

  it("leaves the viewport alone when focus lands outside any editable element", () => {
    dispose = installIosZoomGuard({ nav: probe() });
    const button = document.createElement("button");
    document.body.appendChild(button);

    focus(button);
    expect(meta.content).toBe(BASE_VIEWPORT);
  });

  it("does nothing at all on a non-iOS browser", () => {
    dispose = installIosZoomGuard({ nav: probe({ vendor: "Google Inc." }) });
    const input = addField("input");

    focus(input);
    expect(meta.content).toBe(BASE_VIEWPORT);
  });

  it("does nothing when the document already pins maximum-scale", () => {
    document.head.innerHTML = "";
    addViewportMeta(LOCKED_VIEWPORT);
    dispose = installIosZoomGuard({ nav: probe() });
    const input = addField("input");

    focus(input);
    // Untouched — in particular, not a second `maximum-scale` appended.
    expect(meta.content).toBe(LOCKED_VIEWPORT);
  });

  it("no-ops without a viewport meta rather than throwing", () => {
    document.head.innerHTML = "";
    expect(() => {
      dispose = installIosZoomGuard({ nav: probe() });
    }).not.toThrow();
  });

  it("disposing removes the listeners and restores the viewport", () => {
    const stop = installIosZoomGuard({ nav: probe() });
    const input = addField("input");

    focus(input);
    expect(meta.content).toBe(LOCKED_VIEWPORT);

    stop();
    expect(meta.content).toBe(BASE_VIEWPORT);

    focus(input);
    expect(meta.content).toBe(BASE_VIEWPORT);
  });

  it("disposing mid-focus cancels the pending restore", () => {
    const stop = installIosZoomGuard({ nav: probe() });
    const input = addField("input");

    focus(input);
    blur(input);
    stop();
    vi.runAllTimers();

    expect(meta.content).toBe(BASE_VIEWPORT);
  });

  it("treats an unresolvable font-size as zoom-prone", () => {
    dispose = installIosZoomGuard({ nav: probe() });
    const input = addField("input", "");

    focus(input);
    expect(meta.content).toBe(LOCKED_VIEWPORT);
  });

  // ── Modals, inline forms, and other dynamic DOM ────────────────────────
  // Every field in the app that matters is mounted *after* the guard installs
  // — modals, the inline row editors, EntityPicker's dropdown. The guard
  // delegates from the document rather than binding per-field precisely so
  // those work; these cases pin that property down.

  it("covers a field mounted after the guard installed (modal opened later)", () => {
    dispose = installIosZoomGuard({ nav: probe() });

    const modal = document.createElement("div");
    modal.className = "modal";
    const field = document.createElement("input");
    field.className = "form-control";
    field.style.fontSize = "14px";
    modal.appendChild(field);
    document.body.appendChild(modal);

    focus(field);
    expect(meta.content).toBe(LOCKED_VIEWPORT);
  });

  it("covers a field nested several levels inside a modal", () => {
    dispose = installIosZoomGuard({ nav: probe() });
    document.body.innerHTML = `
      <div class="modal-backdrop"><div class="modal"><div class="form-group">
        <label class="form-label" for="deep">Target</label>
        <input id="deep" class="form-control" style="font-size: 14px">
      </div></div></div>`;

    focus(document.getElementById("deep")!);
    expect(meta.content).toBe(LOCKED_VIEWPORT);
  });

  it("covers a field inside an inline <form>", () => {
    dispose = installIosZoomGuard({ nav: probe() });
    const form = document.createElement("form");
    const field = addField("input");
    form.appendChild(field);
    document.body.appendChild(form);

    focus(field);
    expect(meta.content).toBe(LOCKED_VIEWPORT);
  });

  it("releases the pin when a modal unmounts while its field is focused", () => {
    dispose = installIosZoomGuard({ nav: probe() });
    const modal = document.createElement("div");
    const field = addField("input");
    modal.appendChild(field);
    document.body.appendChild(modal);

    focus(field);
    expect(meta.content).toBe(LOCKED_VIEWPORT);

    // Chromium (and the spec's focus fixup rule) fires focusout on removal.
    blur(field);
    modal.remove();
    vi.runAllTimers();
    expect(meta.content).toBe(BASE_VIEWPORT);
  });

  it("recovers a stuck pin on the next tap, even with no focusout", () => {
    // Engines have historically differed on whether removing a focused element
    // fires focusout. If one does not, the pin would otherwise stay set for the
    // rest of the session — so any tap on a non-zooming target releases it.
    dispose = installIosZoomGuard({ nav: probe() });
    const field = addField("input");

    focus(field);
    field.remove(); // no focusout fired
    vi.runAllTimers();
    expect(meta.content).toBe(LOCKED_VIEWPORT);

    const elsewhere = document.createElement("button");
    document.body.appendChild(elsewhere);
    elsewhere.dispatchEvent(new Event("touchstart", { bubbles: true }));
    vi.runAllTimers();
    expect(meta.content).toBe(BASE_VIEWPORT);
  });

  it("holds the pin when focus moves from a modal field to one behind it", () => {
    dispose = installIosZoomGuard({ nav: probe() });
    const background = addField("input");
    const modalField = addField("input");

    focus(modalField);
    blur(modalField);
    background.focus(); // real focus, so activeElement is set
    focus(background);
    vi.runAllTimers();

    expect(meta.content).toBe(LOCKED_VIEWPORT);
  });

  it("holds the pin when only focusout fires but focus landed on another field", () => {
    // The deferred release re-reads activeElement, so the pin survives even if
    // the paired focusin never arrives to cancel the timer.
    dispose = installIosZoomGuard({ nav: probe() });
    const first = addField("input");
    const second = addField("input");

    focus(first);
    second.focus();
    blur(first);
    vi.runAllTimers();

    expect(meta.content).toBe(LOCKED_VIEWPORT);
  });

  it("defaults to the live document when no document is passed", () => {
    dispose = installIosZoomGuard({ nav: probe() });
    const input = addField("input");

    focus(input);
    expect(document.querySelector<HTMLMetaElement>('meta[name="viewport"]')?.content).toBe(
      LOCKED_VIEWPORT
    );
  });
});
