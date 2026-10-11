import { describe, it, expect, vi } from "vitest";
import { notifyStateChanged, onStateChanged } from "./stateEvents";

describe("stateEvents (#678)", () => {
  it("runs a subscriber when its change is signalled", () => {
    const handler = vi.fn();
    const stop = onStateChanged("vacation-mode", handler);
    notifyStateChanged("vacation-mode");
    expect(handler).toHaveBeenCalledTimes(1);
    stop();
  });

  it("keeps the two kinds of change independent", () => {
    const vacation = vi.fn();
    const eco = vi.fn();
    const stopVacation = onStateChanged("vacation-mode", vacation);
    const stopEco = onStateChanged("eco-suspend", eco);
    notifyStateChanged("eco-suspend");
    expect(eco).toHaveBeenCalledTimes(1);
    expect(vacation).not.toHaveBeenCalled();
    stopVacation();
    stopEco();
  });

  it("stops calling a subscriber once unsubscribed", () => {
    const handler = vi.fn();
    const stop = onStateChanged("eco-suspend", handler);
    stop();
    notifyStateChanged("eco-suspend");
    expect(handler).not.toHaveBeenCalled();
  });
});
