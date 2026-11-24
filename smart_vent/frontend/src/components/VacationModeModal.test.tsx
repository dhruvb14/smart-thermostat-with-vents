// SPDX-License-Identifier: AGPL-3.0-or-later
import { describe, it, expect, vi, beforeEach, afterEach, type Mock } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import VacationModeModal from "./VacationModeModal";
import { TimeZoneContext, buildTimeZoneContext } from "../contexts";
import * as api from "../api";
import { onStateChanged } from "../stateEvents";

vi.mock("../api");

const OFF: api.VacationMode = { enabled: false, return_at: null };
const ON: api.VacationMode = { enabled: true, return_at: "2026-12-25T10:00:00.000Z" };

describe("VacationModeModal — enable flow", () => {
  beforeEach(() => vi.clearAllMocks());

  it("shows enable form when vacation mode is off", () => {
    render(<VacationModeModal current={OFF} onClose={vi.fn()} onChanged={vi.fn()} />);
    expect(screen.getAllByText(/Enable vacation mode/i).length).toBeGreaterThan(0);
    expect(screen.getByLabelText(/Return date/i)).toBeInTheDocument();
    expect(screen.getByText(/all room schedules/i)).toBeInTheDocument();
  });

  it("shows error when no date selected", async () => {
    render(<VacationModeModal current={OFF} onClose={vi.fn()} onChanged={vi.fn()} />);
    fireEvent.click(screen.getByLabelText(/Start immediately/i));
    const btn = screen.getAllByText(/Enable vacation mode/i).find((el) => el.tagName === "BUTTON")!;
    fireEvent.click(btn);
    expect(await screen.findByText(/choose a return date/i)).toBeInTheDocument();
    expect(api.enableVacationMode).not.toHaveBeenCalled();
  });

  it("calls enableVacationMode and onChanged on valid submit", async () => {
    const future = new Date(Date.now() + 86_400_000).toISOString().slice(0, 16);
    const updated: api.VacationMode = { enabled: true, return_at: new Date(future).toISOString() };
    vi.mocked(api.enableVacationMode).mockResolvedValue(updated);

    const onChanged = vi.fn();
    const onClose = vi.fn();
    render(<VacationModeModal current={OFF} onClose={onClose} onChanged={onChanged} />);

    fireEvent.click(screen.getByLabelText(/Start immediately/i));
    fireEvent.change(screen.getByLabelText(/Return date/i), { target: { value: future } });
    const btn = screen.getAllByText(/Enable vacation mode/i).find((el) => el.tagName === "BUTTON")!;
    fireEvent.click(btn);

    // The picked local datetime is normalised to a UTC ISO instant before it
    // reaches the API — a bare toHaveBeenCalled() would not notice the raw
    // "YYYY-MM-DDTHH:mm" string being posted instead.
    await waitFor(() =>
      expect(api.enableVacationMode).toHaveBeenCalledWith(new Date(future).toISOString())
    );
    expect(onChanged).toHaveBeenCalledWith(updated);
    expect(onClose).toHaveBeenCalled();
  });

  it("shows error when past date selected", async () => {
    render(<VacationModeModal current={OFF} onClose={vi.fn()} onChanged={vi.fn()} />);
    const past = new Date(Date.now() - 86_400_000).toISOString().slice(0, 16);
    fireEvent.click(screen.getByLabelText(/Start immediately/i));
    fireEvent.change(screen.getByLabelText(/Return date/i), { target: { value: past } });
    const btn = screen.getAllByText(/Enable vacation mode/i).find((el) => el.tagName === "BUTTON")!;
    fireEvent.click(btn);
    expect(await screen.findByText(/in the future/i)).toBeInTheDocument();
    expect(api.enableVacationMode).not.toHaveBeenCalled();
  });
});

describe("VacationModeModal — house time zone (Issues #294, #729)", () => {
  beforeEach(() => vi.clearAllMocks());
  afterEach(() => vi.useRealTimers());

  const NY = buildTimeZoneContext("America/New_York");

  it("computes the min in the HOUSE's zone, not the browser's", () => {
    vi.useFakeTimers();
    // 07:07Z on 9 June is 03:07 EDT in the house.
    vi.setSystemTime(new Date(Date.UTC(2026, 5, 9, 7, 7, 0)));
    render(
      <TimeZoneContext.Provider value={NY}>
        <VacationModeModal current={OFF} onClose={vi.fn()} onChanged={vi.fn()} />
      </TimeZoneContext.Provider>
    );
    const input = screen.getByLabelText(/Return date/i);
    expect(input.getAttribute("min")).toBe("2026-06-09T03:08");
  });

  it("sends the instant the typed house-local time names", async () => {
    vi.mocked(api.enableVacationMode).mockResolvedValue({
      enabled: true,
      return_at: "2099-01-15T17:30:00.000Z",
    });
    render(
      <TimeZoneContext.Provider value={NY}>
        <VacationModeModal current={OFF} onClose={vi.fn()} onChanged={vi.fn()} />
      </TimeZoneContext.Provider>
    );
    fireEvent.click(screen.getByLabelText(/Start immediately/i));
    fireEvent.change(screen.getByLabelText(/Return date/i), {
      target: { value: "2099-01-15T12:30" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Enable vacation mode/i }));
    // 12:30 EST is 17:30Z, whatever zone the test runner is in.
    await waitFor(() =>
      expect(api.enableVacationMode).toHaveBeenCalledWith("2099-01-15T17:30:00.000Z")
    );
  });

  it("warns when the typed time is skipped by a DST change", () => {
    render(
      <TimeZoneContext.Provider value={NY}>
        <VacationModeModal current={OFF} onClose={vi.fn()} onChanged={vi.fn()} />
      </TimeZoneContext.Provider>
    );
    fireEvent.click(screen.getByLabelText(/Start immediately/i));
    fireEvent.change(screen.getByLabelText(/Return date/i), {
      target: { value: "2099-03-08T02:30" },
    });
    expect(screen.getByRole("status")).toHaveTextContent(/skipped by a daylight-saving change/);
  });

  it("shows the active return time in the house's zone with a zone tooltip", () => {
    render(
      <TimeZoneContext.Provider value={NY}>
        <VacationModeModal
          current={{ enabled: true, return_at: "2099-01-15T17:30:00+00:00" }}
          onClose={vi.fn()}
          onChanged={vi.fn()}
        />
      </TimeZoneContext.Provider>
    );
    const time = document.querySelector("time");
    expect(time).not.toBeNull();
    expect(time!.getAttribute("title")).toBe("EST");
    expect(time!.textContent).toMatch(/12:30/);
  });
});

describe("VacationModeModal — dismiss flow", () => {
  beforeEach(() => vi.clearAllMocks());

  it("shows active state info when vacation mode is on", () => {
    render(<VacationModeModal current={ON} onClose={vi.fn()} onChanged={vi.fn()} />);
    expect(screen.getByText(/Vacation mode active/i)).toBeInTheDocument();
    expect(screen.getByText(/End vacation mode early/i)).toBeInTheDocument();
  });

  it("shows confirmation step before disabling", () => {
    render(<VacationModeModal current={ON} onClose={vi.fn()} onChanged={vi.fn()} />);
    fireEvent.click(screen.getByText(/End vacation mode early/i));
    expect(screen.getByText(/End vacation mode\?/i)).toBeInTheDocument();
    expect(screen.getByText(/normal schedule control immediately/i)).toBeInTheDocument();
    expect(screen.getByText(/Yes, end vacation mode/i)).toBeInTheDocument();
    expect(screen.getByText(/Keep vacation mode/i)).toBeInTheDocument();
  });

  it("cancels back to info view on Keep vacation mode", () => {
    render(<VacationModeModal current={ON} onClose={vi.fn()} onChanged={vi.fn()} />);
    fireEvent.click(screen.getByText(/End vacation mode early/i));
    fireEvent.click(screen.getByText(/Keep vacation mode/i));
    expect(screen.getByText(/Vacation mode active/i)).toBeInTheDocument();
  });

  it("calls disableVacationMode and onChanged when confirmed", async () => {
    const updated: api.VacationMode = { enabled: false, return_at: null };
    vi.mocked(api.disableVacationMode).mockResolvedValue(updated);
    const onChanged = vi.fn();
    const onClose = vi.fn();

    render(<VacationModeModal current={ON} onClose={onClose} onChanged={onChanged} />);
    fireEvent.click(screen.getByText(/End vacation mode early/i));
    fireEvent.click(screen.getByText(/Yes, end vacation mode/i));

    await waitFor(() => expect(api.disableVacationMode).toHaveBeenCalled());
    expect(onChanged).toHaveBeenCalledWith(updated);
    expect(onClose).toHaveBeenCalled();
  });
});

describe("VacationModeModal — app-wide change signal (#678)", () => {
  let signalled: Mock<() => void>;
  let stop: () => void;

  beforeEach(() => {
    vi.clearAllMocks();
    signalled = vi.fn<() => void>();
    stop = onStateChanged("vacation-mode", signalled);
  });
  afterEach(() => stop());

  const enableFor = (value: string) => {
    fireEvent.click(screen.getByLabelText(/Start immediately/i));
    fireEvent.change(screen.getByLabelText(/Return date/i), { target: { value } });
    fireEvent.click(
      screen.getAllByText(/Enable vacation mode/i).find((el) => el.tagName === "BUTTON")!
    );
  };

  it("signals after enabling, so the global banner shows without a reload", async () => {
    vi.mocked(api.enableVacationMode).mockResolvedValue(ON);
    render(<VacationModeModal current={OFF} onClose={vi.fn()} onChanged={vi.fn()} />);
    enableFor(new Date(Date.now() + 86_400_000).toISOString().slice(0, 16));
    await waitFor(() => expect(signalled).toHaveBeenCalledTimes(1));
  });

  it("signals after ending vacation mode", async () => {
    vi.mocked(api.disableVacationMode).mockResolvedValue(OFF);
    render(<VacationModeModal current={ON} onClose={vi.fn()} onChanged={vi.fn()} />);
    fireEvent.click(screen.getByText(/End vacation mode early/i));
    fireEvent.click(screen.getByText(/Yes, end vacation mode/i));
    await waitFor(() => expect(signalled).toHaveBeenCalledTimes(1));
  });

  it("does not signal when the write fails — nothing changed", async () => {
    vi.mocked(api.enableVacationMode).mockRejectedValue(new Error("boom"));
    render(<VacationModeModal current={OFF} onClose={vi.fn()} onChanged={vi.fn()} />);
    enableFor(new Date(Date.now() + 86_400_000).toISOString().slice(0, 16));
    expect(await screen.findByText("boom")).toBeInTheDocument();
    expect(signalled).not.toHaveBeenCalled();
  });
});
