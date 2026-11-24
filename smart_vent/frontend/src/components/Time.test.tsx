// SPDX-License-Identifier: AGPL-3.0-or-later
import { describe, it, expect } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import Time, { HouseTime, ZoneHint } from "./Time";
import DstAdjustedHint from "./DstAdjustedHint";
import { TimeZoneContext, buildTimeZoneContext } from "../contexts";

const NY = buildTimeZoneContext("America/New_York");
const wrap = (ui: React.ReactNode, ctx = NY) =>
  render(<TimeZoneContext.Provider value={ctx}>{ui}</TimeZoneContext.Provider>);

describe("<Time> (Issue #729)", () => {
  it("renders an instant in the house zone with the short zone name as its tooltip", () => {
    wrap(
      <Time
        value="2026-01-15T17:30:00Z"
        kind="time"
        options={{ hour: "2-digit", minute: "2-digit", hour12: false }}
      />
    );
    const el = screen.getByText("12:30");
    expect(el.tagName).toBe("TIME");
    expect(el).toHaveAttribute("title", "EST");
    expect(el).toHaveAttribute("dateTime", "2026-01-15T17:30:00.000Z");
  });

  it("accepts Date and epoch-ms values", () => {
    wrap(
      <>
        <Time value={new Date(Date.UTC(2026, 6, 1, 16))} kind="date" />
        <Time value={Date.UTC(2026, 6, 1, 16)} />
      </>
    );
    const times = document.querySelectorAll("time");
    expect(times).toHaveLength(2);
    times.forEach((t) => expect(t).toHaveAttribute("title", "EDT"));
  });

  it("shows an unparseable value verbatim with no tooltip", () => {
    wrap(<Time value="sometime-next-week" />);
    const el = screen.getByText("sometime-next-week");
    expect(el).not.toHaveAttribute("title");
    expect(el).not.toHaveAttribute("dateTime");
  });

  it("flags the browser zone when the house zone could not be loaded", () => {
    wrap(<Time value="2026-01-15T17:30:00Z" />, buildTimeZoneContext("Asia/Tokyo", "unavailable"));
    expect(document.querySelector("time")).toHaveAttribute(
      "title",
      "UTC+09:00 (browser; house zone unavailable)"
    );
  });
});

describe("<ZoneHint>", () => {
  it("names the zone on hover and toggles it inline on tap", () => {
    wrap(<ZoneHint at="2026-01-15T17:30:00Z" />);
    const btn = screen.getByRole("button", { name: "Times are in EST" });
    expect(btn).toHaveAttribute("title", "EST");
    expect(btn).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(btn);
    expect(btn).toHaveAttribute("aria-expanded", "true");
    expect(btn).toHaveTextContent("EST");
    fireEvent.click(btn);
    expect(btn).toHaveAttribute("aria-expanded", "false");
  });
});

describe("<HouseTime>", () => {
  it("labels already-local text with the zone", () => {
    wrap(<HouseTime>08:00</HouseTime>);
    expect(screen.getByText("08:00")).toHaveAttribute("title", expect.stringMatching(/^E[SD]T$/));
  });
});

describe("<DstAdjustedHint>", () => {
  it("is silent for an empty or ordinary time", () => {
    const { container, rerender } = wrap(<DstAdjustedHint value="" />);
    expect(container).toBeEmptyDOMElement();
    rerender(
      <TimeZoneContext.Provider value={NY}>
        <DstAdjustedHint value="2026-03-08T04:30" />
      </TimeZoneContext.Provider>
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("says out loud when a skipped time will be moved", () => {
    wrap(<DstAdjustedHint value="2026-03-08T02:30" />);
    expect(screen.getByRole("status")).toHaveTextContent(
      /skipped by a daylight-saving change, so it will be saved as .*3:30/
    );
  });
});
