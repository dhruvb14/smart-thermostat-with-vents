import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, render, screen, fireEvent, waitFor } from "@testing-library/react";
import VacationModeBanner from "./VacationModeBanner";
import * as api from "../api";
import { notifyStateChanged } from "../stateEvents";

vi.mock("../api");

function settings(vacation_mode: api.VacationMode): api.AppSettings {
  return {
    temperature_unit: "F",
    theme: "system",
    unit_change_ack_required: false,
    vacation_mode,
    eco_suspend: {},
  };
}

describe("VacationModeBanner", () => {
  beforeEach(() => vi.clearAllMocks());

  it("renders nothing when vacation mode is off", async () => {
    vi.mocked(api.getSettings).mockResolvedValue({
      temperature_unit: "F",
      theme: "system",
      unit_change_ack_required: false,
      vacation_mode: { enabled: false, return_at: null },
      eco_suspend: {},
    });
    const { container } = render(<VacationModeBanner />);
    await waitFor(() => expect(api.getSettings).toHaveBeenCalled());
    expect(container.firstChild).toBeNull();
  });

  it("renders banner when vacation mode is on", async () => {
    vi.mocked(api.getSettings).mockResolvedValue({
      temperature_unit: "F",
      theme: "system",
      unit_change_ack_required: false,
      vacation_mode: { enabled: true, return_at: "2026-12-25T10:00:00.000Z" },
      eco_suspend: {},
    });
    render(<VacationModeBanner />);
    expect(await screen.findByText(/Vacation mode active/i)).toBeInTheDocument();
    expect(screen.getByText(/Manage/i)).toBeInTheDocument();
  });

  it("opens modal when banner is clicked", async () => {
    vi.mocked(api.getSettings).mockResolvedValue({
      temperature_unit: "F",
      theme: "system",
      unit_change_ack_required: false,
      vacation_mode: { enabled: true, return_at: "2026-12-25T10:00:00.000Z" },
      eco_suspend: {},
    });
    render(<VacationModeBanner />);
    const banner = await screen.findByRole("alert");
    fireEvent.click(banner);
    expect(screen.getAllByText(/Vacation mode active/i).length).toBeGreaterThan(0);
    expect(screen.getByText(/End vacation mode early/i)).toBeInTheDocument();
  });

  it("hides the banner after vacation mode is ended through the modal", async () => {
    // Active on the first read only — like the server, the refetch the modal's
    // change signal triggers (#678) reads the post-DELETE state.
    vi.mocked(api.getSettings)
      .mockResolvedValueOnce(settings({ enabled: true, return_at: "2026-12-25T10:00:00.000Z" }))
      .mockResolvedValue(settings({ enabled: false, return_at: null }));
    vi.mocked(api.disableVacationMode).mockResolvedValue({ enabled: false, return_at: null });
    const { container } = render(<VacationModeBanner />);
    fireEvent.click(await screen.findByRole("alert"));
    fireEvent.click(screen.getByText(/End vacation mode early/i));
    fireEvent.click(screen.getByText(/Yes, end vacation mode/i));
    await waitFor(() => expect(api.disableVacationMode).toHaveBeenCalled());
    // onChanged({enabled: false}) both closes the modal and removes the banner.
    await waitFor(() => expect(container.firstChild).toBeNull());
  });

  it("closes the modal but keeps the banner when it is dismissed", async () => {
    vi.mocked(api.getSettings).mockResolvedValue({
      temperature_unit: "F",
      theme: "system",
      unit_change_ack_required: false,
      vacation_mode: { enabled: true, return_at: "2026-12-25T10:00:00.000Z" },
      eco_suspend: {},
    });
    render(<VacationModeBanner />);
    fireEvent.click(await screen.findByRole("alert"));
    fireEvent.click(screen.getByText("Close"));
    expect(screen.queryByText(/End vacation mode early/i)).not.toBeInTheDocument();
    expect(screen.getByRole("alert")).toBeInTheDocument();
  });

  it("opens modal when Manage button is clicked", async () => {
    vi.mocked(api.getSettings).mockResolvedValue({
      temperature_unit: "F",
      theme: "system",
      unit_change_ack_required: false,
      vacation_mode: { enabled: true, return_at: "2026-12-25T10:00:00.000Z" },
      eco_suspend: {},
    });
    render(<VacationModeBanner />);
    fireEvent.click(await screen.findByText(/Manage/i));
    // The banner itself reads "Vacation mode active", so matching that text
    // proves nothing about the modal — assert on markup only the modal renders.
    expect(screen.getByText(/End vacation mode early/i)).toBeInTheDocument();
    expect(screen.getByText(/Normal scheduling will resume automatically/i)).toBeInTheDocument();
  });

  it("appears when vacation mode is enabled elsewhere, without a reload (#678)", async () => {
    // The banner is mounted once for the life of the app. Before #678 it only
    // read on mount, so enabling vacation from the Dashboard left it missing
    // until a full reload — a force-close in the PWA.
    vi.mocked(api.getSettings)
      .mockResolvedValueOnce(settings({ enabled: false, return_at: null }))
      .mockResolvedValue(settings({ enabled: true, return_at: "2026-12-25T10:00:00.000Z" }));
    render(<VacationModeBanner />);
    await waitFor(() => expect(api.getSettings).toHaveBeenCalledTimes(1));
    expect(screen.queryByTestId("vacation-mode-banner")).toBeNull();

    act(() => notifyStateChanged("vacation-mode"));

    expect(await screen.findByTestId("vacation-mode-banner")).toBeInTheDocument();
  });

  it("disappears when vacation mode ends elsewhere, e.g. at its return time (#678)", async () => {
    vi.mocked(api.getSettings)
      .mockResolvedValueOnce(settings({ enabled: true, return_at: "2026-12-25T10:00:00.000Z" }))
      .mockResolvedValue(settings({ enabled: false, return_at: null }));
    render(<VacationModeBanner />);
    await screen.findByTestId("vacation-mode-banner");

    act(() => notifyStateChanged("vacation-mode"));

    await waitFor(() => expect(screen.queryByTestId("vacation-mode-banner")).toBeNull());
  });

  it("ignores an older response that lands after a newer one (#678)", async () => {
    let resolveMount!: (s: api.AppSettings) => void;
    vi.mocked(api.getSettings)
      .mockReturnValueOnce(new Promise((resolve) => (resolveMount = resolve)))
      .mockResolvedValue(settings({ enabled: true, return_at: "2026-12-25T10:00:00.000Z" }));
    render(<VacationModeBanner />);

    // The refetch is issued after the mount read and resolves first…
    act(() => notifyStateChanged("vacation-mode"));
    await screen.findByTestId("vacation-mode-banner");

    // …so the mount read's pre-change answer must not hide the banner again.
    await act(async () => resolveMount(settings({ enabled: false, return_at: null })));
    expect(screen.getByTestId("vacation-mode-banner")).toBeInTheDocument();
  });

  it("stops listening once unmounted (#678)", async () => {
    vi.mocked(api.getSettings).mockResolvedValue(settings({ enabled: false, return_at: null }));
    const { unmount } = render(<VacationModeBanner />);
    await waitFor(() => expect(api.getSettings).toHaveBeenCalledTimes(1));
    unmount();
    notifyStateChanged("vacation-mode");
    expect(api.getSettings).toHaveBeenCalledTimes(1);
  });

  it("falls back to the raw return_at string when it is not a parseable date (#497)", async () => {
    vi.mocked(api.getSettings).mockResolvedValue({
      temperature_unit: "F",
      theme: "system",
      unit_change_ack_required: false,
      vacation_mode: { enabled: true, return_at: "sometime-next-week" },
      eco_suspend: {},
    });
    render(<VacationModeBanner />);
    // `new Date()` never throws — without the NaN guard this rendered the
    // literal string "Invalid Date" instead of the stored raw value.
    expect(await screen.findByText(/Returning sometime-next-week\./)).toBeInTheDocument();
    expect(screen.queryByText(/Invalid Date/)).toBeNull();
  });
});
