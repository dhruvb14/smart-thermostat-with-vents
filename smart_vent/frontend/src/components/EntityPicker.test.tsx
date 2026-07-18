import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import EntityPicker from "./EntityPicker";
import * as api from "../api";

vi.mock("../api");

const ENTITIES = [
  { entity_id: "sensor.hallway_temp", state: "70.1", friendly_name: "Hallway Temperature" },
  { entity_id: "sensor.attic_temp", state: "80.4", friendly_name: "Attic Temperature" },
  { entity_id: "weather.home", state: "sunny", friendly_name: "Home Weather" },
];

describe("EntityPicker", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getHAEntities).mockResolvedValue(ENTITIES);
  });

  it("builds the default placeholder from a string domain", () => {
    render(<EntityPicker domain="sensor" onSelect={vi.fn()} />);
    expect(screen.getByPlaceholderText("Search sensor entities…")).toBeInTheDocument();
  });

  it("joins array domains in the default placeholder", () => {
    render(<EntityPicker domain={["sensor", "weather"]} onSelect={vi.fn()} />);
    expect(screen.getByPlaceholderText("Search sensor / weather entities…")).toBeInTheDocument();
  });

  it("prefers an explicit placeholder", () => {
    render(<EntityPicker domain="sensor" placeholder="Pick one…" onSelect={vi.fn()} />);
    expect(screen.getByPlaceholderText("Pick one…")).toBeInTheDocument();
  });

  it("opens the dropdown on focus and lists fetched entities", async () => {
    render(<EntityPicker domain="sensor" onSelect={vi.fn()} />);
    await waitFor(() => expect(api.getHAEntities).toHaveBeenCalled());
    fireEvent.focus(screen.getByRole("textbox"));
    expect(await screen.findByText("Hallway Temperature")).toBeInTheDocument();
    expect(screen.getByText("sensor.attic_temp")).toBeInTheDocument();
  });

  it("filters by friendly name and entity id, case-insensitively", async () => {
    render(<EntityPicker domain="sensor" onSelect={vi.fn()} />);
    await waitFor(() => expect(api.getHAEntities).toHaveBeenCalled());
    const input = screen.getByRole("textbox");
    fireEvent.change(input, { target: { value: "ATTIC" } });
    expect(await screen.findByText("Attic Temperature")).toBeInTheDocument();
    expect(screen.queryByText("Hallway Temperature")).not.toBeInTheDocument();

    // entity_id matching too
    fireEvent.change(input, { target: { value: "weather.home" } });
    expect(await screen.findByText("Home Weather")).toBeInTheDocument();
  });

  it("selects an entity on mousedown, clears the query, and closes the dropdown", async () => {
    const onSelect = vi.fn();
    render(<EntityPicker domain="sensor" onSelect={onSelect} />);
    await waitFor(() => expect(api.getHAEntities).toHaveBeenCalled());
    const input = screen.getByRole("textbox") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "hall" } });
    fireEvent.mouseDown(await screen.findByText("Hallway Temperature"));
    expect(onSelect).toHaveBeenCalledWith("sensor.hallway_temp");
    expect(input.value).toBe("");
    expect(screen.queryByText("Hallway Temperature")).not.toBeInTheDocument();
  });

  it("closes the dropdown when clicking outside the picker", async () => {
    render(
      <div>
        <EntityPicker domain="sensor" onSelect={vi.fn()} />
        <button>elsewhere</button>
      </div>
    );
    await waitFor(() => expect(api.getHAEntities).toHaveBeenCalled());
    fireEvent.focus(screen.getByRole("textbox"));
    expect(await screen.findByText("Hallway Temperature")).toBeInTheDocument();
    fireEvent.mouseDown(screen.getByRole("button", { name: "elsewhere" }));
    expect(screen.queryByText("Hallway Temperature")).not.toBeInTheDocument();
  });

  it("renders no dropdown when the entity fetch fails", async () => {
    vi.mocked(api.getHAEntities).mockRejectedValue(new Error("HA down"));
    const { container } = render(<EntityPicker domain="sensor" onSelect={vi.fn()} />);
    await waitFor(() => expect(api.getHAEntities).toHaveBeenCalled());
    fireEvent.focus(screen.getByRole("textbox"));
    expect(container.querySelector(".entity-dropdown")).toBeNull();
  });
});
