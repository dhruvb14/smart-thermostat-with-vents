import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { EcoWorkedExample } from "./EcoMode";
import { UnitContext, buildUnitContext } from "../contexts";

// Eco Mode live worked example (Issue #404). The component is unit-agnostic:
// feed it values in whatever unit the surrounding form holds and it echoes the
// same unit back. These tests pin the cooling + heating example text and the
// relaxed targets it computes via ``ecoRelaxedTarget``.

describe("EcoWorkedExample (#404)", () => {
  it("renders the cooling and heating examples in Fahrenheit", () => {
    // Matches the °F thermostat defaults. A 70°F room:
    //  cooling: outside=fullDrift(100) → 70 + 4 = 74
    //  heating: outside=fullDrift(0)  → 70 - 4 = 66
    render(
      <UnitContext.Provider value={buildUnitContext("F")}>
        <EcoWorkedExample
          params={{
            coolingThreshold: 86,
            coolingFullDrift: 100,
            coolingMaxDrift: 4,
            heatingThreshold: 40,
            heatingFullDrift: 0,
            heatingMaxDrift: 4,
          }}
        />
      </UnitContext.Provider>
    );

    const box = screen.getByTestId("eco-worked-example");
    expect(box).toHaveTextContent(/Cooling example:/);
    expect(box).toHaveTextContent(/Heating example:/);
    // Sample indoor target, thresholds, and relaxed targets.
    expect(box).toHaveTextContent("70°F");
    expect(box).toHaveTextContent("86°F"); // cooling threshold
    expect(box).toHaveTextContent("74°F"); // cooling relaxed
    expect(box).toHaveTextContent("40°F"); // heating threshold
    expect(box).toHaveTextContent("66°F"); // heating relaxed
  });

  it("uses a 21°C sample room and °C labels in Celsius", () => {
    // A 21°C room, params in °C:
    //  cooling: outside=fullDrift(38) → 21 + 2 = 23
    //  heating: outside=fullDrift(-18) → 21 - 2 = 19
    render(
      <UnitContext.Provider value={buildUnitContext("C")}>
        <EcoWorkedExample
          params={{
            coolingThreshold: 30,
            coolingFullDrift: 38,
            coolingMaxDrift: 2,
            heatingThreshold: 4,
            heatingFullDrift: -18,
            heatingMaxDrift: 2,
          }}
        />
      </UnitContext.Provider>
    );

    const box = screen.getByTestId("eco-worked-example");
    expect(box).toHaveTextContent(/Cooling example:/);
    expect(box).toHaveTextContent(/Heating example:/);
    expect(box).toHaveTextContent("21°C"); // sample indoor target
    expect(box).toHaveTextContent("30°C"); // cooling threshold
    expect(box).toHaveTextContent("23°C"); // cooling relaxed
    expect(box).toHaveTextContent("4°C"); // heating threshold
    expect(box).toHaveTextContent("19°C"); // heating relaxed
  });
});
