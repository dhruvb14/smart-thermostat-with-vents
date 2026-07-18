import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import ChartContainer, { ChartSkeleton } from "./ChartContainer";

// recharts' ResponsiveContainer measures its parent via ResizeObserver, which
// reports a 0×0 box in jsdom and refuses to render children. Replace it with a
// pass-through so we can assert the children actually mount.
vi.mock("recharts", async () => {
  const actual = await vi.importActual<typeof import("recharts")>("recharts");
  return {
    ...actual,
    ResponsiveContainer: ({ children }: { children: React.ReactNode }) => (
      <div data-testid="responsive-container">{children}</div>
    ),
  };
});

describe("ChartContainer", () => {
  it("renders title and children when data is present", () => {
    render(
      <ChartContainer title="Duty cycle">
        <svg data-testid="the-chart" />
      </ChartContainer>
    );
    expect(screen.getByText("Duty cycle")).toBeInTheDocument();
    expect(screen.getByTestId("the-chart")).toBeInTheDocument();
    expect(screen.queryByText(/No data for this range yet/)).not.toBeInTheDocument();
  });

  it("renders subtitle, action slot, and note when provided", () => {
    render(
      <ChartContainer
        title="Overshoot"
        subtitle="per completed cycle"
        action={<button>Download</button>}
        note="Timeline shows commanded positions."
      >
        <svg />
      </ChartContainer>
    );
    expect(screen.getByText("per completed cycle")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Download" })).toBeInTheDocument();
    expect(screen.getByText("Timeline shows commanded positions.")).toBeInTheDocument();
  });

  it("shows the default empty-state text and no chart when empty", () => {
    render(
      <ChartContainer title="Empty chart" empty>
        <svg data-testid="the-chart" />
      </ChartContainer>
    );
    expect(screen.getByText("No data for this range yet.")).toBeInTheDocument();
    expect(screen.queryByTestId("the-chart")).not.toBeInTheDocument();
  });

  it("shows custom empty text when supplied", () => {
    render(
      <ChartContainer title="Empty chart" empty emptyText="Nothing recorded.">
        <svg />
      </ChartContainer>
    );
    expect(screen.getByText("Nothing recorded.")).toBeInTheDocument();
  });

  it("renders the skeleton (not children or empty state) while loading", () => {
    const { container } = render(
      <ChartContainer title="Loading chart" loading empty>
        <svg data-testid="the-chart" />
      </ChartContainer>
    );
    expect(container.querySelectorAll(".skeleton-bar").length).toBeGreaterThan(0);
    expect(screen.queryByTestId("the-chart")).not.toBeInTheDocument();
    expect(screen.queryByText(/No data for this range yet/)).not.toBeInTheDocument();
  });
});

describe("ChartSkeleton", () => {
  it("renders seven aria-hidden bars at the requested height", () => {
    const { container } = render(<ChartSkeleton height={150} />);
    const wrapper = container.firstElementChild as HTMLElement;
    expect(wrapper).toHaveAttribute("aria-hidden");
    expect(wrapper.style.height).toBe("150px");
    expect(container.querySelectorAll(".skeleton-bar")).toHaveLength(7);
  });
});
