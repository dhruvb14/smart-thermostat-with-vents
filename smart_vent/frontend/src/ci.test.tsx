import { render, screen } from "@testing-library/react";
import { describe, it, expect, vi, afterEach } from "vitest";

// isCI is a module-level constant derived from import.meta.env.VITE_APP_VERSION,
// which Vitest leaves unset by default — so the default import is the
// production (non-CI) build. The CI branch is exercised by stubbing the env var
// and re-importing the module with a fresh module registry.

describe("Frozen — production (non-CI) build", () => {
  it("renders children unchanged", async () => {
    const { Frozen, isCI } = await import("./ci");
    expect(isCI).toBe(false);
    render(<Frozen>live value</Frozen>);
    expect(screen.getByText("live value")).toBeInTheDocument();
  });

  it("ignores the frozen prop and still renders children", async () => {
    const { Frozen } = await import("./ci");
    render(<Frozen frozen={<span>placeholder</span>}>live value</Frozen>);
    expect(screen.getByText("live value")).toBeInTheDocument();
    expect(screen.queryByText("placeholder")).not.toBeInTheDocument();
  });
});

describe("Frozen — CI build", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.resetModules();
  });

  it("flags isCI when VITE_APP_VERSION is CI", async () => {
    vi.resetModules();
    vi.stubEnv("VITE_APP_VERSION", "CI");
    const { isCI } = await import("./ci");
    expect(isCI).toBe(true);
  });

  it("renders the default placeholder instead of children", async () => {
    vi.resetModules();
    vi.stubEnv("VITE_APP_VERSION", "CI");
    const { Frozen, FROZEN } = await import("./ci");
    render(<Frozen>live value</Frozen>);
    expect(screen.queryByText("live value")).not.toBeInTheDocument();
    expect(screen.getByText(FROZEN)).toBeInTheDocument();
  });

  it("renders a custom frozen node", async () => {
    vi.resetModules();
    vi.stubEnv("VITE_APP_VERSION", "CI");
    const { Frozen } = await import("./ci");
    render(<Frozen frozen={<span>frozen-node</span>}>live value</Frozen>);
    expect(screen.getByText("frozen-node")).toBeInTheDocument();
    expect(screen.queryByText("live value")).not.toBeInTheDocument();
  });

  it("renders nothing when frozen is null", async () => {
    vi.resetModules();
    vi.stubEnv("VITE_APP_VERSION", "CI");
    const { Frozen } = await import("./ci");
    const { container } = render(
      <div data-testid="wrap">
        <Frozen frozen={null}>live value</Frozen>
      </div>
    );
    expect(container.querySelector('[data-testid="wrap"]')?.textContent).toBe("");
  });
});
