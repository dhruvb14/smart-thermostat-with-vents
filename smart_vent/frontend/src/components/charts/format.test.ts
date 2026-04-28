import { describe, it, expect } from "vitest";
import * as format from "./format";

describe("Chart Formatting Utilities", () => {
  it("fmtSecondsAsHm formats correctly", () => {
    expect(format.fmtSecondsAsHm(0)).toBe("0m");
    expect(format.fmtSecondsAsHm(60)).toBe("1m");
    expect(format.fmtSecondsAsHm(3600)).toBe("1h 0m");
    expect(format.fmtSecondsAsHm(3660)).toBe("1h 1m");
    expect(format.fmtSecondsAsHm(null)).toBe("—");
  });

  it("fmtMinutes formats correctly", () => {
    expect(format.fmtMinutes(60)).toBe("1m");
    expect(format.fmtMinutes(90)).toBe("2m");
    expect(format.fmtMinutes(null)).toBe("—");
  });

  it("fmtPercent formats correctly", () => {
    expect(format.fmtPercent(10.5)).toBe("10.5%");
    expect(format.fmtPercent(10.556, 2)).toBe("10.56%");
    expect(format.fmtPercent(null)).toBe("—");
  });

  it("fmtTemperature formats correctly", () => {
    expect(format.fmtTemperature(72)).toBe("72.0°F");
    expect(format.fmtTemperature(null)).toBe("—");
  });

  it("shortDayLabel strips year", () => {
    expect(format.shortDayLabel("2024-01-01")).toBe("01-01");
    expect(format.shortDayLabel("other")).toBe("other");
  });
});
