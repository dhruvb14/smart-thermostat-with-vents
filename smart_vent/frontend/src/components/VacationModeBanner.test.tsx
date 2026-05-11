import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import VacationModeBanner from "./VacationModeBanner";
import * as api from "../api";

vi.mock("../api");

describe("VacationModeBanner", () => {
  beforeEach(() => vi.clearAllMocks());

  it("renders nothing when vacation mode is off", async () => {
    vi.mocked(api.getSettings).mockResolvedValue({
      temperature_unit: "F",
      unit_change_ack_required: false,
      vacation_mode: { enabled: false, return_at: null },
    });
    const { container } = render(<VacationModeBanner />);
    await waitFor(() => expect(api.getSettings).toHaveBeenCalled());
    expect(container.firstChild).toBeNull();
  });

  it("renders banner when vacation mode is on", async () => {
    vi.mocked(api.getSettings).mockResolvedValue({
      temperature_unit: "F",
      unit_change_ack_required: false,
      vacation_mode: { enabled: true, return_at: "2026-12-25T10:00:00.000Z" },
    });
    render(<VacationModeBanner />);
    expect(await screen.findByText(/Vacation mode active/i)).toBeInTheDocument();
    expect(screen.getByText(/Manage/i)).toBeInTheDocument();
  });

  it("opens modal when banner is clicked", async () => {
    vi.mocked(api.getSettings).mockResolvedValue({
      temperature_unit: "F",
      unit_change_ack_required: false,
      vacation_mode: { enabled: true, return_at: "2026-12-25T10:00:00.000Z" },
    });
    render(<VacationModeBanner />);
    const banner = await screen.findByRole("alert");
    fireEvent.click(banner);
    expect(screen.getAllByText(/Vacation mode active/i).length).toBeGreaterThan(0);
    expect(screen.getByText(/End vacation mode early/i)).toBeInTheDocument();
  });

  it("opens modal when Manage button is clicked", async () => {
    vi.mocked(api.getSettings).mockResolvedValue({
      temperature_unit: "F",
      unit_change_ack_required: false,
      vacation_mode: { enabled: true, return_at: "2026-12-25T10:00:00.000Z" },
    });
    render(<VacationModeBanner />);
    fireEvent.click(await screen.findByText(/Manage/i));
    expect(screen.getAllByText(/Vacation mode active/i).length).toBeGreaterThan(0);
  });
});
