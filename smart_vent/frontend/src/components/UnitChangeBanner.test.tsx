import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import UnitChangeBanner from "./UnitChangeBanner";
import * as api from "../api";

vi.mock("../api");

describe("UnitChangeBanner", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders nothing when ack is not required", async () => {
    vi.mocked(api.getSettings).mockResolvedValue({
      temperature_unit: "F",
      unit_change_ack_required: false,
    });
    const { container } = render(<UnitChangeBanner />);
    await waitFor(() => expect(api.getSettings).toHaveBeenCalled());
    expect(container.firstChild).toBeNull();
  });

  it("renders banner when ack is required", async () => {
    vi.mocked(api.getSettings).mockResolvedValue({
      temperature_unit: "C",
      unit_change_ack_required: true,
    });
    render(<UnitChangeBanner />);
    expect(await screen.findByText(/temperature unit has changed/i)).toBeInTheDocument();
    expect(screen.getByText("Restart Plenum")).toBeInTheDocument();
    expect(screen.getByText("I've reviewed my settings")).toBeInTheDocument();
  });

  it("calls restartApp when Restart Plenum is clicked", async () => {
    vi.mocked(api.getSettings).mockResolvedValue({
      temperature_unit: "C",
      unit_change_ack_required: true,
    });
    vi.mocked(api.restartApp).mockResolvedValue({ restarting: true });
    render(<UnitChangeBanner />);
    fireEvent.click(await screen.findByText("Restart Plenum"));
    await waitFor(() => expect(api.restartApp).toHaveBeenCalled());
  });

  it("calls ackUnitChange and hides banner when dismiss is clicked", async () => {
    vi.mocked(api.getSettings).mockResolvedValue({
      temperature_unit: "C",
      unit_change_ack_required: true,
    });
    vi.mocked(api.ackUnitChange).mockResolvedValue({ ok: true });
    const { container } = render(<UnitChangeBanner />);
    fireEvent.click(await screen.findByText("I've reviewed my settings"));
    await waitFor(() => expect(api.ackUnitChange).toHaveBeenCalled());
    await waitFor(() => expect(container.firstChild).toBeNull());
  });

  it("hides banner on getSettings error", async () => {
    vi.mocked(api.getSettings).mockRejectedValue(new Error("network error"));
    const { container } = render(<UnitChangeBanner />);
    await waitFor(() => expect(api.getSettings).toHaveBeenCalled());
    expect(container.firstChild).toBeNull();
  });
});
