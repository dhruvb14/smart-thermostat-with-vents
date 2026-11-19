import type { VacationMode, VacationState } from "./api";

/**
 * The vacation state (Issue #728), derived for payloads that predate `state`.
 *
 * Lives outside api.ts on purpose: tests `vi.mock("../api")`, which would
 * auto-mock a helper defined there into returning undefined.
 */
export function vacationState(vm: VacationMode | null | undefined): VacationState {
  if (!vm) return "off";
  return vm.state ?? (vm.enabled ? "active" : "off");
}
