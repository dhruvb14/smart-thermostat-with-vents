// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Scheduled vacation and welcome home in the UI (Issue #728): the modal's
 * create / schedule / edit / start-now / cancel / end-without-welcome flows,
 * every banner variant, and the vacationState helper.
 *
 * Rendered in a New York house while vitest runs in Asia/Tokyo, so every
 * expected instant below is the New York reading of what is typed (#729).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act, render, screen, fireEvent, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import VacationModeModal from "./VacationModeModal";
import VacationModeBanner from "./VacationModeBanner";
import { TimeZoneContext, buildTimeZoneContext } from "../contexts";
import { vacationState } from "../vacation";
import * as api from "../api";

vi.mock("../api");

const NY = buildTimeZoneContext("America/New_York");
const inNY = (ui: ReactElement) =>
  render(<TimeZoneContext.Provider value={NY}>{ui}</TimeZoneContext.Provider>);

const OFF: api.VacationMode = { enabled: false, return_at: null, state: "off", start_at: null };
const SCHEDULED: api.VacationMode = {
  enabled: false,
  state: "scheduled",
  start_at: "2099-01-10T13:00:00.000Z", // 08:00 EST
  return_at: "2099-01-17T23:00:00.000Z", // 18:00 EST
};
const ACTIVE: api.VacationMode = {
  enabled: true,
  state: "active",
  start_at: null,
  return_at: "2099-01-17T23:00:00.000Z",
};

function settings(vacation_mode: api.VacationMode): api.AppSettings {
  return {
    temperature_unit: "F",
    theme: "system",
    unit_change_ack_required: false,
    vacation_mode,
    eco_suspend: {},
  };
}

const button = (name: RegExp) => screen.getByRole("button", { name });

describe("vacationState", () => {
  it("prefers the explicit state and falls back to enabled", () => {
    expect(vacationState(null)).toBe("off");
    expect(vacationState({ enabled: true, return_at: null })).toBe("active");
    expect(vacationState({ enabled: false, return_at: null })).toBe("off");
    expect(vacationState(SCHEDULED)).toBe("scheduled");
  });
});

describe("VacationModeModal — scheduling a trip (#728)", () => {
  beforeEach(() => vi.clearAllMocks());

  it("asks for a start and a return by default", () => {
    inNY(<VacationModeModal current={OFF} onClose={vi.fn()} onChanged={vi.fn()} />);
    expect(screen.getByLabelText(/Start date/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/Return date/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/Start immediately/i)).not.toBeChecked();
    expect(button(/Schedule vacation/i)).toBeInTheDocument();
  });

  it("Start immediately hides the start field", () => {
    inNY(<VacationModeModal current={OFF} onClose={vi.fn()} onChanged={vi.fn()} />);
    fireEvent.click(screen.getByLabelText(/Start immediately/i));
    expect(screen.queryByLabelText(/Start date/i)).toBeNull();
    expect(button(/Enable vacation mode/i)).toBeInTheDocument();
  });

  it("requires a start unless starting immediately", async () => {
    inNY(<VacationModeModal current={OFF} onClose={vi.fn()} onChanged={vi.fn()} />);
    fireEvent.click(button(/Schedule vacation/i));
    expect(await screen.findByText(/choose when vacation mode should start/i)).toBeInTheDocument();
    expect(api.enableVacationMode).not.toHaveBeenCalled();
  });

  it("requires a return once a start is set", async () => {
    inNY(<VacationModeModal current={OFF} onClose={vi.fn()} onChanged={vi.fn()} />);
    fireEvent.change(screen.getByLabelText(/Start date/i), {
      target: { value: "2099-01-10T08:00" },
    });
    fireEvent.click(button(/Schedule vacation/i));
    expect(await screen.findByText(/choose a return date/i)).toBeInTheDocument();
  });

  it("sends the house-zone start and return", async () => {
    const onChanged = vi.fn();
    const onClose = vi.fn();
    vi.mocked(api.enableVacationMode).mockResolvedValue(SCHEDULED);
    inNY(<VacationModeModal current={OFF} onClose={onClose} onChanged={onChanged} />);
    fireEvent.change(screen.getByLabelText(/Start date/i), {
      target: { value: "2099-01-10T08:00" },
    });
    // The return picker cannot go earlier than the chosen start.
    expect(screen.getByLabelText(/Return date/i)).toHaveAttribute("min", "2099-01-10T08:01");
    fireEvent.change(screen.getByLabelText(/Return date/i), {
      target: { value: "2099-01-17T18:00" },
    });
    fireEvent.click(button(/Schedule vacation/i));
    await waitFor(() =>
      expect(api.enableVacationMode).toHaveBeenCalledWith(
        "2099-01-17T23:00:00.000Z",
        "2099-01-10T13:00:00.000Z"
      )
    );
    expect(onChanged).toHaveBeenCalledWith(SCHEDULED);
    expect(onClose).toHaveBeenCalled();
  });

  it.each([
    ["2001-01-10T08:00", "2099-01-17T18:00", /Start date must be in the future/],
    ["2099-01-17T18:00", "2099-01-17T18:00", /Return date must be after the start date/],
  ])("rejects start %s / return %s", async (start, ret, message) => {
    inNY(<VacationModeModal current={OFF} onClose={vi.fn()} onChanged={vi.fn()} />);
    fireEvent.change(screen.getByLabelText(/Start date/i), { target: { value: start } });
    fireEvent.change(screen.getByLabelText(/Return date/i), { target: { value: ret } });
    fireEvent.click(button(/Schedule vacation/i));
    expect(await screen.findByText(message)).toBeInTheDocument();
    expect(api.enableVacationMode).not.toHaveBeenCalled();
  });

  it("falls back to generic copy when scheduling rejects with a non-Error", async () => {
    vi.mocked(api.enableVacationMode).mockRejectedValue("nope");
    inNY(<VacationModeModal current={OFF} onClose={vi.fn()} onChanged={vi.fn()} />);
    fireEvent.change(screen.getByLabelText(/Start date/i), {
      target: { value: "2099-01-10T08:00" },
    });
    fireEvent.change(screen.getByLabelText(/Return date/i), {
      target: { value: "2099-01-17T18:00" },
    });
    fireEvent.click(button(/Schedule vacation/i));
    expect(await screen.findByText("Failed to schedule vacation mode")).toBeInTheDocument();
  });
});

describe("VacationModeModal — a scheduled trip (#728)", () => {
  beforeEach(() => vi.clearAllMocks());

  it("summarises the trip in house time", () => {
    inNY(<VacationModeModal current={SCHEDULED} onClose={vi.fn()} onChanged={vi.fn()} />);
    expect(screen.getByText("Vacation scheduled")).toBeInTheDocument();
    const times = Array.from(document.querySelectorAll("time")).map((t) => t.textContent);
    expect(times).toEqual([
      NY.format("2099-01-10T13:00:00.000Z"),
      NY.format("2099-01-17T23:00:00.000Z"),
    ]);
    expect(times[0]).toContain("8:00:00 AM");
  });

  it("Start now posts the return alone", async () => {
    vi.mocked(api.enableVacationMode).mockResolvedValue(ACTIVE);
    const onChanged = vi.fn();
    inNY(<VacationModeModal current={SCHEDULED} onClose={vi.fn()} onChanged={onChanged} />);
    fireEvent.click(button(/Start now/i));
    await waitFor(() =>
      expect(api.enableVacationMode).toHaveBeenCalledWith("2099-01-17T23:00:00.000Z")
    );
    expect(onChanged).toHaveBeenCalledWith(ACTIVE);
  });

  it("Start now is unavailable without a return time", () => {
    inNY(
      <VacationModeModal
        current={{ ...SCHEDULED, start_at: null, return_at: null }}
        onClose={vi.fn()}
        onChanged={vi.fn()}
      />
    );
    expect(button(/Start now/i)).toBeDisabled();
    expect(document.querySelectorAll("time")).toHaveLength(0);
  });

  it("cancelling asks first, and Keep it backs out", async () => {
    vi.mocked(api.disableVacationMode).mockResolvedValue(OFF);
    const onChanged = vi.fn();
    inNY(<VacationModeModal current={SCHEDULED} onClose={vi.fn()} onChanged={onChanged} />);
    fireEvent.click(button(/Cancel scheduled vacation/i));
    expect(screen.getByText(/Cancel scheduled vacation\?/)).toBeInTheDocument();
    fireEvent.click(button(/Keep it/i));
    expect(screen.getByText("Vacation scheduled")).toBeInTheDocument();
    expect(api.disableVacationMode).not.toHaveBeenCalled();

    fireEvent.click(button(/Cancel scheduled vacation/i));
    fireEvent.click(button(/Yes, cancel it/i));
    await waitFor(() => expect(api.disableVacationMode).toHaveBeenCalledWith());
    expect(onChanged).toHaveBeenCalledWith(OFF);
  });

  it("shows a cancel failure inside the confirmation", async () => {
    vi.mocked(api.disableVacationMode).mockRejectedValue(new Error("Server down"));
    inNY(<VacationModeModal current={SCHEDULED} onClose={vi.fn()} onChanged={vi.fn()} />);
    fireEvent.click(button(/Cancel scheduled vacation/i));
    fireEvent.click(button(/Yes, cancel it/i));
    expect(await screen.findByText("Server down")).toBeInTheDocument();
    expect(screen.getByText(/Cancel scheduled vacation\?/)).toBeInTheDocument();
  });

  it("Edit dates prefills the house wall clock and saves both", async () => {
    vi.mocked(api.enableVacationMode).mockResolvedValue(SCHEDULED);
    inNY(<VacationModeModal current={SCHEDULED} onClose={vi.fn()} onChanged={vi.fn()} />);
    fireEvent.click(button(/Edit dates/i));
    expect(screen.getByText("Edit scheduled vacation")).toBeInTheDocument();
    expect(screen.queryByLabelText(/Start immediately/i)).toBeNull();
    expect(screen.getByLabelText(/Start date/i)).toHaveValue("2099-01-10T08:00");
    expect(screen.getByLabelText(/Return date/i)).toHaveValue("2099-01-17T18:00");

    fireEvent.click(button(/^Back$/));
    expect(screen.getByText("Vacation scheduled")).toBeInTheDocument();

    fireEvent.click(button(/Edit dates/i));
    fireEvent.change(screen.getByLabelText(/Start date/i), {
      target: { value: "2099-01-11T08:00" },
    });
    fireEvent.click(button(/Save dates/i));
    await waitFor(() =>
      expect(api.enableVacationMode).toHaveBeenCalledWith(
        "2099-01-17T23:00:00.000Z",
        "2099-01-11T13:00:00.000Z"
      )
    );
  });

  it("closes from the backdrop", () => {
    const onClose = vi.fn();
    inNY(<VacationModeModal current={SCHEDULED} onClose={onClose} onChanged={vi.fn()} />);
    fireEvent.click(document.querySelector(".modal-backdrop")!);
    expect(onClose).toHaveBeenCalled();
  });
});

describe("VacationModeModal — ending an active trip (#728)", () => {
  beforeEach(() => vi.clearAllMocks());

  it("offers ending without welcome home", async () => {
    vi.mocked(api.disableVacationMode).mockResolvedValue(OFF);
    inNY(<VacationModeModal current={ACTIVE} onClose={vi.fn()} onChanged={vi.fn()} />);
    fireEvent.click(button(/End vacation mode early/i));
    expect(screen.getByText(/Welcome home/)).toBeInTheDocument();
    fireEvent.click(button(/End without welcome home/i));
    await waitFor(() => expect(api.disableVacationMode).toHaveBeenCalledWith(false));
  });
});

describe("VacationModeBanner — scheduled, heads-up, welcome home, missed (#728)", () => {
  beforeEach(() => vi.clearAllMocks());
  afterEach(() => vi.useRealTimers());

  it("shows a scheduled trip with both dates", async () => {
    vi.mocked(api.getSettings).mockResolvedValue(settings(SCHEDULED));
    inNY(<VacationModeBanner />);
    const banner = await screen.findByTestId("vacation-scheduled-banner");
    expect(banner).toHaveTextContent(
      `Starts ${NY.format(SCHEDULED.start_at!)}, returns ${NY.format(SCHEDULED.return_at!)}.`
    );
    expect(banner).not.toHaveTextContent(/starts in/);
    expect(screen.queryByTestId("vacation-mode-banner")).toBeNull();
    fireEvent.click(banner);
    expect(
      screen.getByText("Vacation scheduled", { selector: ".modal-title" })
    ).toBeInTheDocument();
  });

  it("Manage opens the modal without the card's own click", async () => {
    vi.mocked(api.getSettings).mockResolvedValue(settings(SCHEDULED));
    inNY(<VacationModeBanner />);
    await screen.findByTestId("vacation-scheduled-banner");
    fireEvent.click(button(/Manage/i));
    expect(button(/Start now/i)).toBeInTheDocument();
  });

  it("tolerates a scheduled payload missing its dates", async () => {
    vi.mocked(api.getSettings).mockResolvedValue(
      settings({ ...SCHEDULED, start_at: null, return_at: null })
    );
    inNY(<VacationModeBanner />);
    const banner = await screen.findByTestId("vacation-scheduled-banner");
    expect(banner).toHaveTextContent("Starts , returns .");
  });

  it.each([
    [100, "Vacation mode starts in 1 h 40 min"],
    [120, "Vacation mode starts in 2 h"],
    [25, "Vacation mode starts in 25 min"],
  ])("turns into a heads-up %i minutes before the start", async (minutes, text) => {
    const start = new Date(Date.now() + minutes * 60_000 - 1_000).toISOString();
    vi.mocked(api.getSettings).mockResolvedValue(settings({ ...SCHEDULED, start_at: start }));
    inNY(<VacationModeBanner />);
    const banner = await screen.findByTestId("vacation-scheduled-banner");
    expect(banner).toHaveTextContent(text);
    expect(banner).toHaveStyle({ borderLeft: "3px solid var(--orange)" });
  });

  it("re-renders the countdown every 30 seconds", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval", "Date"] });
    vi.setSystemTime(new Date("2099-01-10T11:00:00.000Z")); // 2 h before the start
    vi.mocked(api.getSettings).mockResolvedValue(settings(SCHEDULED));
    inNY(<VacationModeBanner />);
    const banner = await screen.findByTestId("vacation-scheduled-banner");
    expect(banner).toHaveTextContent("starts in 2 h");
    act(() => {
      vi.setSystemTime(new Date("2099-01-10T12:30:00.000Z"));
      vi.advanceTimersByTime(30_000);
    });
    expect(banner).toHaveTextContent("starts in 30 min");
  });

  it("shows the welcome-home window while it is open", async () => {
    const until = new Date(Date.now() + 45 * 60_000).toISOString();
    vi.mocked(api.getSettings).mockResolvedValue(
      settings({ ...ACTIVE, welcome_home_until: until })
    );
    inNY(<VacationModeBanner />);
    const banner = await screen.findByTestId("welcome-home-banner");
    expect(banner).toHaveTextContent("Rooms are coming back to temperature until");
    expect(screen.getByTestId("vacation-mode-banner")).toBeInTheDocument();
  });

  it("shows welcome home after the trip ended, and nothing once it closed", async () => {
    const until = new Date(Date.now() + 45 * 60_000).toISOString();
    vi.mocked(api.getSettings).mockResolvedValue(settings({ ...OFF, welcome_home_until: until }));
    const { unmount } = inNY(<VacationModeBanner />);
    expect(await screen.findByTestId("welcome-home-banner")).toBeInTheDocument();
    unmount();

    const past = new Date(Date.now() - 60_000).toISOString();
    vi.mocked(api.getSettings).mockResolvedValue(settings({ ...OFF, welcome_home_until: past }));
    const { container } = inNY(<VacationModeBanner />);
    await waitFor(() => expect(api.getSettings).toHaveBeenCalledTimes(2));
    expect(container).toBeEmptyDOMElement();
  });

  it("shows a missed trip until it is dismissed", async () => {
    const missed = {
      start_at: "2099-01-10T13:00:00+00:00",
      return_at: "2099-01-11T13:00:00+00:00",
    };
    vi.mocked(api.getSettings).mockResolvedValue(settings({ ...OFF, missed }));
    vi.mocked(api.dismissVacationMissedNotice).mockImplementation(async () => {
      // The refetch the dismissal triggers sees the cleared notice too.
      vi.mocked(api.getSettings).mockResolvedValue(settings(OFF));
      return OFF;
    });
    inNY(<VacationModeBanner />);
    const banner = await screen.findByTestId("vacation-missed-banner");
    expect(banner).toHaveTextContent(
      `A scheduled vacation (${NY.format(missed.start_at)} – ${NY.format(missed.return_at)}) was missed while Plenum was offline`
    );
    fireEvent.click(button(/Dismiss/i));
    await waitFor(() => expect(screen.queryByTestId("vacation-missed-banner")).toBeNull());
  });

  it("keeps the missed notice when dismissing fails", async () => {
    const missed = {
      start_at: "2099-01-10T13:00:00+00:00",
      return_at: "2099-01-11T13:00:00+00:00",
    };
    vi.mocked(api.getSettings).mockResolvedValue(settings({ ...OFF, missed }));
    vi.mocked(api.dismissVacationMissedNotice).mockRejectedValue(new Error("x"));
    inNY(<VacationModeBanner />);
    await screen.findByTestId("vacation-missed-banner");
    fireEvent.click(button(/Dismiss/i));
    await waitFor(() => expect(api.dismissVacationMissedNotice).toHaveBeenCalled());
    expect(screen.getByTestId("vacation-missed-banner")).toBeInTheDocument();
  });
});
