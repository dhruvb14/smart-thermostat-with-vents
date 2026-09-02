import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";
import { createPortal } from "react-dom";
import { installIosZoomGuard } from "./iosZoomGuard";
import HoldModal from "./components/HoldModal";
import VacationModeModal from "./components/VacationModeModal";
import EntityPicker from "./components/EntityPicker";
import * as api from "../src/api";
import { ecoRoomDefaults } from "./testFixtures";

vi.mock("../src/api");

// ---------------------------------------------------------------------------
// The unit suite (iosZoomGuard.test.ts) drives synthetic elements with inline
// font sizes. This suite drives the components the user actually taps — the
// modals, the inline pickers — through the real `.form-control` rules, so a
// change to either the markup or the stylesheet shows up here.
// ---------------------------------------------------------------------------

const BASE_VIEWPORT = "width=device-width, initial-scale=1.0";
const LOCKED_VIEWPORT = `${BASE_VIEWPORT}, maximum-scale=1`;

const IOS = {
  userAgent:
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 " +
    "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1",
  vendor: "Apple Computer, Inc.",
  maxTouchPoints: 5,
};

// The shipped stylesheet, read from disk: Vite stubs a `?raw` CSS import to
// an empty string under the test transform, so the file is loaded directly.
const STYLES = readFileSync(join(import.meta.dirname, "styles.css"), "utf8");

/** The `font-size` a bare `<html>` resolves `rem` against. */
const ROOT_FONT_PX = 16;

/**
 * The px `font-size` styles.css declares for `selector`.
 *
 * Read from the shipped stylesheet rather than hard-coded, so this suite tracks
 * the real values instead of a copy that can drift away from them.
 */
function declaredFontSizePx(selector: string): number {
  for (const [, sel, body] of STYLES.matchAll(/([^{}]+)\{([^}]*)\}/g)) {
    if (sel.trim() !== selector) continue;
    const size = /font-size:\s*([\d.]+)(px|rem)\s*;/.exec(body);
    if (size) {
      return size[2] === "rem" ? Number(size[1]) * ROOT_FONT_PX : Number(size[1]);
    }
  }
  throw new Error(`styles.css declares no font-size for "${selector}"`);
}

/**
 * Install the real form-control sizes as px.
 *
 * jsdom applies the cascade but does not resolve `rem`, and the guard only
 * trusts a px computed size (which is all a real engine ever reports). So the
 * rem values are resolved here, from the real stylesheet, and re-declared in
 * the unit the guard will actually see in a browser.
 */
function applyFormControlStyles() {
  const style = document.createElement("style");
  style.textContent = `
    .form-control { font-size: ${declaredFontSizePx(".form-control")}px; }
    .form-control-sm { font-size: ${declaredFontSizePx(".form-control-sm")}px; }
  `;
  document.head.appendChild(style);
}

let meta: HTMLMetaElement;
let dispose: () => void;

/** A real focus (so `activeElement` is set) plus the event the guard listens for. */
function focusField(el: HTMLElement) {
  el.focus();
  fireEvent.focusIn(el);
}

function blurField(el: HTMLElement) {
  el.blur();
  fireEvent.focusOut(el);
}

const viewport = () => meta.content;

beforeEach(() => {
  vi.useFakeTimers();
  vi.clearAllMocks();
  meta = document.createElement("meta");
  meta.name = "viewport";
  meta.content = BASE_VIEWPORT;
  document.head.appendChild(meta);
  applyFormControlStyles();
  dispose = installIosZoomGuard({ nav: IOS });
});

afterEach(() => {
  cleanup();
  dispose();
  vi.useRealTimers();
  document.head.innerHTML = "";
  document.body.innerHTML = "";
});

describe("the form-control sizes this guard exists for", () => {
  // If either of these ever reaches 16, iOS stops auto-zooming that control on
  // its own and the guard is no longer what is holding the behaviour up.
  it("are both under the 16px threshold in styles.css", () => {
    expect(declaredFontSizePx(".form-control")).toBe(14);
    expect(declaredFontSizePx(".form-control-sm")).toBeCloseTo(12.8);
    expect(declaredFontSizePx(".form-control")).toBeLessThan(16);
    expect(declaredFontSizePx(".form-control-sm")).toBeLessThan(16);
  });

  it("pins the viewport at each of them", () => {
    for (const cls of ["form-control", "form-control form-control-sm"]) {
      const field = document.createElement("input");
      field.className = cls;
      document.body.appendChild(field);

      focusField(field);
      expect(viewport()).toBe(LOCKED_VIEWPORT);

      blurField(field);
      vi.runAllTimers();
      expect(viewport()).toBe(BASE_VIEWPORT);
    }
  });
});

describe("modals", () => {
  const room = (over: Partial<api.Room> = {}): api.Room =>
    ({
      id: "room-1",
      name: "Living Room",
      thermostat_entity_id: "climate.test",
      include_thermostat_sensor: false,
      system_wide_temp: null,
      presence_holdover_hours: 2,
      notes: "",
      temp_offset: 0,
      deadband_override: null,
      ambient_suppression_enabled: false,
      ambient_suppression_mode: "any_presence",
      ambient_suppression_min_differential: 5,
      ambient_suppression_deadband: 2,
      ambient_suppression_off_schedule_window_min: 60,
      ...ecoRoomDefaults,
      ...over,
    }) as api.Room;

  it("pins for the hold modal's text input", () => {
    render(<HoldModal rooms={[room()]} holds={{}} onClose={vi.fn()} onChanged={vi.fn()} />);

    focusField(screen.getByLabelText(/hold temperature/i));
    expect(viewport()).toBe(LOCKED_VIEWPORT);
  });

  it("pins for the hold modal's <select>", () => {
    render(<HoldModal rooms={[room()]} holds={{}} onClose={vi.fn()} onChanged={vi.fn()} />);

    focusField(screen.getByLabelText("Room"));
    expect(viewport()).toBe(LOCKED_VIEWPORT);
  });

  it("holds the pin while the user moves between two fields of one modal", () => {
    render(<HoldModal rooms={[room()]} holds={{}} onClose={vi.fn()} onChanged={vi.fn()} />);
    const roomPicker = screen.getByLabelText("Room");
    const target = screen.getByLabelText(/hold temperature/i);

    focusField(roomPicker);
    blurField(roomPicker);
    focusField(target);
    vi.runAllTimers();

    expect(viewport()).toBe(LOCKED_VIEWPORT);
  });

  it("releases the pin when the modal closes with a field focused", () => {
    const { unmount } = render(
      <HoldModal rooms={[room()]} holds={{}} onClose={vi.fn()} onChanged={vi.fn()} />
    );
    const target = screen.getByLabelText(/hold temperature/i);

    focusField(target);
    expect(viewport()).toBe(LOCKED_VIEWPORT);

    // React unmounts the tree; the engine's focus fixup fires focusout.
    blurField(target);
    unmount();
    vi.runAllTimers();

    expect(viewport()).toBe(BASE_VIEWPORT);
  });

  it("pins for the vacation-mode modal's datetime field", () => {
    render(
      <VacationModeModal
        current={{ enabled: false, return_at: null }}
        onClose={vi.fn()}
        onChanged={vi.fn()}
      />
    );

    focusField(screen.getByLabelText(/return date/i));
    expect(viewport()).toBe(LOCKED_VIEWPORT);
  });
});

describe("inline forms and pickers", () => {
  it("pins for the inline entity picker", () => {
    vi.mocked(api.getHAEntities).mockResolvedValue([]);
    render(<EntityPicker domain="sensor" onSelect={vi.fn()} />);

    focusField(screen.getByPlaceholderText(/search sensor entities/i));
    expect(viewport()).toBe(LOCKED_VIEWPORT);
  });

  it("pins for a field rendered through a React portal", () => {
    // ConfirmDialog and the Logs detail pane portal out of their parent; a
    // portalled field is still in the document, so the delegated listener sees
    // it — but only because the guard binds to the document, not to a subtree.
    function Portalled() {
      return createPortal(<input className="form-control" aria-label="portalled" />, document.body);
    }
    render(<Portalled />);

    focusField(screen.getByLabelText("portalled"));
    expect(viewport()).toBe(LOCKED_VIEWPORT);
  });

  it("leaves the viewport alone for a modal's checkbox and buttons", () => {
    render(
      <HoldModal
        rooms={[
          {
            id: "room-1",
            name: "Living Room",
            thermostat_entity_id: "climate.test",
            include_thermostat_sensor: false,
            system_wide_temp: null,
            presence_holdover_hours: 2,
            notes: "",
            temp_offset: 0,
            deadband_override: null,
            ambient_suppression_enabled: false,
            ambient_suppression_mode: "any_presence",
            ambient_suppression_min_differential: 5,
            ambient_suppression_deadband: 2,
            ambient_suppression_off_schedule_window_min: 60,
            ...ecoRoomDefaults,
          } as api.Room,
        ]}
        holds={{}}
        onClose={vi.fn()}
        onChanged={vi.fn()}
      />
    );

    for (const el of screen.getAllByRole("checkbox").concat(screen.getAllByRole("button"))) {
      focusField(el);
      vi.runAllTimers();
      expect(viewport()).toBe(BASE_VIEWPORT);
    }
  });
});
