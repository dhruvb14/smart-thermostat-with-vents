import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import VacationModeModal from "./VacationModeModal";
import * as api from "../api";

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

    fireEvent.change(screen.getByLabelText(/Return date/i), { target: { value: future } });
    const btn = screen.getAllByText(/Enable vacation mode/i).find((el) => el.tagName === "BUTTON")!;
    fireEvent.click(btn);

    await waitFor(() => expect(api.enableVacationMode).toHaveBeenCalled());
    expect(onChanged).toHaveBeenCalledWith(updated);
    expect(onClose).toHaveBeenCalled();
  });

  it("shows error when past date selected", async () => {
    render(<VacationModeModal current={OFF} onClose={vi.fn()} onChanged={vi.fn()} />);
    const past = new Date(Date.now() - 86_400_000).toISOString().slice(0, 16);
    fireEvent.change(screen.getByLabelText(/Return date/i), { target: { value: past } });
    const btn = screen.getAllByText(/Enable vacation mode/i).find((el) => el.tagName === "BUTTON")!;
    fireEvent.click(btn);
    expect(await screen.findByText(/in the future/i)).toBeInTheDocument();
    expect(api.enableVacationMode).not.toHaveBeenCalled();
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
