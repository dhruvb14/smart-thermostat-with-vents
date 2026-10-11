/**
 * In-page "this server state just changed" signals (#678).
 *
 * Vacation mode and Eco Suspend are shown in several places at once: the global
 * banners rendered in App.tsx above every route, and the Dashboard / Thermostats
 * controls that open the shared modals. Each of those holds its own copy of the
 * state, so a change made through one has to tell the others — before this the
 * banners fetched once on mount, and enabling vacation mode from the Dashboard
 * left the banner missing until a full reload (a force-close in the PWA).
 *
 * Two sources raise a signal:
 * - the shared modals, as soon as their write succeeds, so the tab that made
 *   the change updates even when its WebSocket is mid-reconnect (a PWA resumed
 *   from the background);
 * - AppRoot's WebSocket handler, relaying the backend's `vacation_mode_changed`
 *   / `eco_suspend_changed` broadcasts — which covers every change made
 *   elsewhere: another tab or device, MCP, MQTT, or the expiry sweep ending a
 *   vacation or a suspension on schedule.
 *
 * Listeners just refetch, so a change that arrives through both paths costs one
 * extra GET and nothing else.
 */

export type StateChange = "vacation-mode" | "eco-suspend";

const EVENT_NAMES: Record<StateChange, string> = {
  "vacation-mode": "plenum-vacation-mode-changed",
  "eco-suspend": "plenum-eco-suspend-changed",
};

export function notifyStateChanged(change: StateChange): void {
  window.dispatchEvent(new CustomEvent(EVENT_NAMES[change]));
}

/**
 * Run `handler` whenever `change` is signalled. Returns the unsubscribe
 * function, so it can be returned straight from a `useEffect`.
 */
export function onStateChanged(change: StateChange, handler: () => void): () => void {
  const listener = () => handler();
  window.addEventListener(EVENT_NAMES[change], listener);
  return () => window.removeEventListener(EVENT_NAMES[change], listener);
}
