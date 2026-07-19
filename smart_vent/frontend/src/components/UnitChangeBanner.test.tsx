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
      theme: "system",
      unit_change_ack_required: false,
      vacation_mode: { enabled: false, return_at: null },
      eco_suspend: {},
    });
    const { container } = render(<UnitChangeBanner />);
    await waitFor(() => expect(api.getSettings).toHaveBeenCalled());
    expect(container.firstChild).toBeNull();
  });

  it("renders banner when ack is required", async () => {
    vi.mocked(api.getSettings).mockResolvedValue({
      temperature_unit: "C",
      theme: "system",
      unit_change_ack_required: true,
      vacation_mode: { enabled: false, return_at: null },
      eco_suspend: {},
    });
    render(<UnitChangeBanner />);
    expect(await screen.findByText(/temperature unit changed to °C/i)).toBeInTheDocument();
    expect(screen.getByText(/Min \/ max setpoints/i)).toBeInTheDocument();
    expect(screen.getByText("Restart Plenum")).toBeInTheDocument();
    expect(screen.getByText("I've reviewed my settings")).toBeInTheDocument();
  });

  it("calls restartApp when Restart Plenum is clicked", async () => {
    vi.mocked(api.getSettings).mockResolvedValue({
      temperature_unit: "C",
      theme: "system",
      unit_change_ack_required: true,
      vacation_mode: { enabled: false, return_at: null },
      eco_suspend: {},
    });
    vi.mocked(api.restartApp).mockResolvedValue({ restarting: true });
    render(<UnitChangeBanner />);
    fireEvent.click(await screen.findByText("Restart Plenum"));
    await waitFor(() => expect(api.restartApp).toHaveBeenCalled());
  });

  it("calls ackUnitChange and hides banner when dismiss is clicked", async () => {
    vi.mocked(api.getSettings).mockResolvedValue({
      temperature_unit: "C",
      theme: "system",
      unit_change_ack_required: true,
      vacation_mode: { enabled: false, return_at: null },
      eco_suspend: {},
    });
    vi.mocked(api.ackUnitChange).mockResolvedValue({ ok: true });
    const { container } = render(<UnitChangeBanner />);
    fireEvent.click(await screen.findByText("I've reviewed my settings"));
    await waitFor(() => expect(api.ackUnitChange).toHaveBeenCalled());
    await waitFor(() => expect(container.firstChild).toBeNull());
  });

  it("re-enables the restart button when restartApp fails", async () => {
    vi.mocked(api.getSettings).mockResolvedValue({
      temperature_unit: "C",
      theme: "system",
      unit_change_ack_required: true,
      vacation_mode: { enabled: false, return_at: null },
      eco_suspend: {},
    });
    vi.mocked(api.restartApp).mockRejectedValue(new Error("restart failed"));
    render(<UnitChangeBanner />);
    const button = await screen.findByText("Restart Plenum");
    fireEvent.click(button);
    await waitFor(() => expect(api.restartApp).toHaveBeenCalled());
    // The catch path resets `restarting`, so the button returns to its idle
    // label and is clickable again.
    await waitFor(() => expect(screen.getByText("Restart Plenum")).toBeEnabled());
  });

  it("keeps the banner and re-enables dismiss when ackUnitChange fails", async () => {
    vi.mocked(api.getSettings).mockResolvedValue({
      temperature_unit: "C",
      theme: "system",
      unit_change_ack_required: true,
      vacation_mode: { enabled: false, return_at: null },
      eco_suspend: {},
    });
    vi.mocked(api.ackUnitChange).mockRejectedValue(new Error("ack failed"));
    render(<UnitChangeBanner />);
    fireEvent.click(await screen.findByText("I've reviewed my settings"));
    await waitFor(() => expect(api.ackUnitChange).toHaveBeenCalled());
    // Banner must NOT hide if the ack never persisted.
    expect(screen.getByRole("alert")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText("I've reviewed my settings")).toBeEnabled());
  });

  it("hides banner on getSettings error", async () => {
    vi.mocked(api.getSettings).mockRejectedValue(new Error("network error"));
    const { container } = render(<UnitChangeBanner />);
    await waitFor(() => expect(api.getSettings).toHaveBeenCalled());
    expect(container.firstChild).toBeNull();
  });
});
