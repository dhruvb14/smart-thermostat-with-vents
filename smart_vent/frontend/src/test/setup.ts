import "@testing-library/jest-dom";
import { vi } from "vitest";

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

(globalThis as any).location = mockLocation;
(window as any).location = mockLocation;

// Mock ResizeObserver
class ResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

(window as any).ResizeObserver = ResizeObserver;
