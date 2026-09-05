import { describe, it, expect } from "vitest";
import { formatCountdown } from "./countdown";

// formatCountdown has no test file of its own — it is only exercised indirectly
// by the pages that render holds (Dashboard, Rooms, Schedules, HoldModal), and
// those never drive it past its boundaries. These cover each arm directly.

describe("formatCountdown", () => {
  it("reports an already-elapsed hold as ending, not as a negative time", () => {
    // The hold cards poll a server-computed `ends_in_seconds`; between the poll
    // and the render it can have gone past zero. Anything <= 0 must read as
    // "ending…" rather than "0s" or "-1m -5s".
    expect(formatCountdown(0)).toBe("ending…");
    expect(formatCountdown(-1)).toBe("ending…");
    expect(formatCountdown(-3661)).toBe("ending…");
  });

  it("shows seconds only under a minute", () => {
    expect(formatCountdown(1)).toBe("1s");
    expect(formatCountdown(59)).toBe("59s");
  });

  it("shows minutes and seconds under an hour", () => {
    expect(formatCountdown(60)).toBe("1m 0s");
    expect(formatCountdown(95)).toBe("1m 35s");
    expect(formatCountdown(3599)).toBe("59m 59s");
  });

  it("shows hours and minutes at an hour and above, dropping seconds", () => {
    expect(formatCountdown(3600)).toBe("1h 0m");
    expect(formatCountdown(3600 + 25 * 60 + 30)).toBe("1h 25m");
    // An 8-hour hold (the Rooms page's longest preset).
    expect(formatCountdown(8 * 3600)).toBe("8h 0m");
    expect(formatCountdown(100 * 3600 + 59 * 60)).toBe("100h 59m");
  });

  it("switches format exactly at the minute and hour boundaries", () => {
    expect(formatCountdown(59)).toBe("59s");
    expect(formatCountdown(60)).toBe("1m 0s");
    expect(formatCountdown(3599)).toBe("59m 59s");
    expect(formatCountdown(3600)).toBe("1h 0m");
  });
});
