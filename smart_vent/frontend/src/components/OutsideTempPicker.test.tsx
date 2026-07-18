import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import OutsideTempPicker from "./OutsideTempPicker";
import { UnitContext, buildUnitContext } from "../contexts";
import * as api from "../api";

vi.mock("../api");

function renderPicker(unit: "F" | "C" = "F", onChange?: (id: string | null) => void) {
  return render(
    <UnitContext.Provider value={buildUnitContext(unit)}>
      <OutsideTempPicker onChange={onChange} />
    </UnitContext.Provider>
  );
}

describe("OutsideTempPicker", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getHAEntities).mockResolvedValue([
      { entity_id: "sensor.outdoor", state: "50", friendly_name: "Outdoor Sensor" },
    ]);
  });

  it("shows the configured entity with its formatted reading and fires onChange on load", async () => {
    vi.mocked(api.getOutsideTempEntity).mockResolvedValue({
      entity_id: "sensor.outdoor",
      current_value: 50,
    });
    const onChange = vi.fn();
    renderPicker("F", onChange);
    expect(await screen.findByText(/sensor\.outdoor/)).toBeInTheDocument();
    // EntityState-style value is °F; fmtTemp renders it with the unit label.
    expect(screen.getByText(/50\.0°F/)).toBeInTheDocument();
    await waitFor(() => expect(onChange).toHaveBeenCalledWith("sensor.outdoor"));
  });

  it("converts the current reading for display in Celsius mode", async () => {
    vi.mocked(api.getOutsideTempEntity).mockResolvedValue({
      entity_id: "sensor.outdoor",
      current_value: 50, // °F from the backend — display should be 10°C
    });
    renderPicker("C");
    expect(await screen.findByText(/10\.0°C/)).toBeInTheDocument();
  });

  it("shows the none-configured placeholder when no entity is set", async () => {
    vi.mocked(api.getOutsideTempEntity).mockResolvedValue({
      entity_id: null,
      current_value: null,
    });
    renderPicker();
    expect(await screen.findByText(/None configured/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Clear" })).not.toBeInTheDocument();
  });

  it("saves a selection made through the entity picker", async () => {
    vi.mocked(api.getOutsideTempEntity).mockResolvedValue({
      entity_id: null,
      current_value: null,
    });
    vi.mocked(api.setOutsideTempEntity).mockResolvedValue({
      entity_id: "sensor.outdoor",
      current_value: 41,
    });
    const onChange = vi.fn();
    renderPicker("F", onChange);
    await screen.findByText(/None configured/);

    fireEvent.focus(screen.getByRole("textbox"));
    fireEvent.mouseDown(await screen.findByText("Outdoor Sensor"));

    await waitFor(() => expect(api.setOutsideTempEntity).toHaveBeenCalledWith("sensor.outdoor"));
    expect(await screen.findByText(/sensor\.outdoor/)).toBeInTheDocument();
    expect(onChange).toHaveBeenLastCalledWith("sensor.outdoor");
  });

  it("clears the configured entity via the Clear button", async () => {
    vi.mocked(api.getOutsideTempEntity).mockResolvedValue({
      entity_id: "sensor.outdoor",
      current_value: 50,
    });
    vi.mocked(api.setOutsideTempEntity).mockResolvedValue({
      entity_id: null,
      current_value: null,
    });
    const onChange = vi.fn();
    renderPicker("F", onChange);

    fireEvent.click(await screen.findByRole("button", { name: "Clear" }));
    await waitFor(() => expect(api.setOutsideTempEntity).toHaveBeenCalledWith(null));
    expect(await screen.findByText(/None configured/)).toBeInTheDocument();
    expect(onChange).toHaveBeenLastCalledWith(null);
  });

  it("surfaces a load failure as an error badge", async () => {
    vi.mocked(api.getOutsideTempEntity).mockRejectedValue(new Error("boom on load"));
    renderPicker();
    expect(await screen.findByText("boom on load")).toBeInTheDocument();
  });

  it("surfaces a save failure and keeps the previous entity", async () => {
    vi.mocked(api.getOutsideTempEntity).mockResolvedValue({
      entity_id: "sensor.outdoor",
      current_value: 50,
    });
    vi.mocked(api.setOutsideTempEntity).mockRejectedValue(new Error("save exploded"));
    renderPicker();

    fireEvent.click(await screen.findByRole("button", { name: "Clear" }));
    expect(await screen.findByText("save exploded")).toBeInTheDocument();
    expect(screen.getByText(/sensor\.outdoor/)).toBeInTheDocument();
  });

  it("recovers from a load error once a save succeeds (#497)", async () => {
    vi.mocked(api.getOutsideTempEntity).mockRejectedValue(new Error("boom on load"));
    vi.mocked(api.setOutsideTempEntity).mockResolvedValue({
      entity_id: "sensor.outdoor",
      current_value: 94,
    });
    renderPicker();
    expect(await screen.findByText("boom on load")).toBeInTheDocument();

    fireEvent.change(screen.getByPlaceholderText(/Search/i), {
      target: { value: "outdoor" },
    });
    fireEvent.mouseDown(await screen.findByText(/Outdoor Sensor/));
    expect(await screen.findByText(/94\.0°F/)).toBeInTheDocument();
    expect(screen.queryByText("boom on load")).toBeNull();
  });
});
