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

  // The filter ORs two clauses (entity_id, friendly_name), so a query that
  // matches both proves neither: dropping either clause — or making either one
  // case-sensitive — still leaves the other one matching, and the test passes.
  // Every query below is therefore chosen to hit exactly ONE clause.
  it("filters by friendly name on text that is not in the entity id", async () => {
    render(<EntityPicker domain="sensor" onSelect={vi.fn()} />);
    await waitFor(() => expect(api.getHAEntities).toHaveBeenCalled());
    const input = screen.getByRole("textbox");

    // "temperature" is spelled out only in the friendly names; the entity ids
    // are abbreviated to "_temp", so the entity_id clause cannot match here.
    fireEvent.change(input, { target: { value: "temperature" } });
    expect(await screen.findByText("Attic Temperature")).toBeInTheDocument();
    expect(screen.getByText("Hallway Temperature")).toBeInTheDocument();
    // …and the entity whose friendly name lacks the word is filtered out, so
    // this is a real filter and not just "everything still renders".
    expect(screen.queryByText("Home Weather")).not.toBeInTheDocument();
  });

  it("filters by entity id on text that is not in the friendly name", async () => {
    render(<EntityPicker domain="sensor" onSelect={vi.fn()} />);
    await waitFor(() => expect(api.getHAEntities).toHaveBeenCalled());
    const input = screen.getByRole("textbox");

    // The "sensor." domain prefix appears in no friendly name.
    fireEvent.change(input, { target: { value: "sensor.attic" } });
    expect(await screen.findByText("Attic Temperature")).toBeInTheDocument();
    expect(screen.queryByText("Hallway Temperature")).not.toBeInTheDocument();
    expect(screen.queryByText("Home Weather")).not.toBeInTheDocument();
  });

  it("matches case-insensitively on each clause independently", async () => {
    render(<EntityPicker domain="sensor" onSelect={vi.fn()} />);
    await waitFor(() => expect(api.getHAEntities).toHaveBeenCalled());
    const input = screen.getByRole("textbox");

    // Upper-cased entity-id text. Entity ids are lower-case in HA, so this can
    // only match if the entity_id clause folds case — the friendly names
    // contain no "sensor." prefix to fall back on.
    fireEvent.change(input, { target: { value: "SENSOR.ATTIC" } });
    expect(await screen.findByText("Attic Temperature")).toBeInTheDocument();
    expect(screen.queryByText("Hallway Temperature")).not.toBeInTheDocument();

    // Lower-cased friendly-name text, spelled out so no entity id contains it.
    fireEvent.change(input, { target: { value: "hallway temperature" } });
    expect(await screen.findByText("Hallway Temperature")).toBeInTheDocument();
    expect(screen.queryByText("Attic Temperature")).not.toBeInTheDocument();
  });

  it("shows no dropdown when nothing matches", async () => {
    const { container } = render(<EntityPicker domain="sensor" onSelect={vi.fn()} />);
    await waitFor(() => expect(api.getHAEntities).toHaveBeenCalled());
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "zzz-no-such-entity" } });
    await waitFor(() => expect(container.querySelector(".entity-dropdown")).toBeNull());
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
