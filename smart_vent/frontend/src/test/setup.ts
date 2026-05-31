import { vi, expect } from "vitest";
import { createElement } from "react";
import * as matchers from "@testing-library/jest-dom/matchers";
import type { TestingLibraryMatchers } from "@testing-library/jest-dom/matchers";

expect.extend(matchers);

declare module "vitest" {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any, @typescript-eslint/no-empty-object-type
  interface Assertion<T = any> extends TestingLibraryMatchers<any, T> {}
  // eslint-disable-next-line @typescript-eslint/no-explicit-any, @typescript-eslint/no-empty-object-type
  interface AsymmetricMatchersContaining extends TestingLibraryMatchers<any, any> {}
}

// recharts' ResponsiveContainer measures its parent via ResizeObserver, which
// reports a 0×0 box in jsdom — recharts then logs "The width(0) and height(0)
// of chart should be greater than 0" for every chart under test. Globally
// replace it with a fixed-size box so charts render their children cleanly and
// the surrounding component logic is still exercised. (Charts assert on titles/
// labels, not pixel geometry, so a fixed size is sufficient.)
vi.mock("recharts", async () => {
  const actual = await vi.importActual<typeof import("recharts")>("recharts");
  return {
    ...actual,
    ResponsiveContainer: ({ children }: { children: React.ReactNode }) =>
      createElement("div", { style: { width: 800, height: 300 } }, children),
  };
});

// Mock matchMedia
Object.defineProperty(window, "matchMedia", {
  writable: true,
  value: vi.fn().mockImplementation((query) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: vi.fn(), // deprecated
    removeListener: vi.fn(), // deprecated
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })),
});

// Mock scrollIntoView
window.HTMLElement.prototype.scrollIntoView = vi.fn();

// Mock location in global
const mockLocation = {
  pathname: "/",
  protocol: "http:",
  host: "localhost",
};

vi.stubGlobal("location", mockLocation);

// Mock ResizeObserver
class ResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

(window as unknown as { ResizeObserver: typeof ResizeObserver }).ResizeObserver = ResizeObserver;
