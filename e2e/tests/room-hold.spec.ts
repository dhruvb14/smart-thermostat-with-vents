import { test, expect, captureModal } from "./fixtures";

/**
 * Visual + interaction coverage of temporary temperature holds (Issue #576).
 *
 * A hold pins one room at an exact temperature for 1–8 hours, taking
 * precedence over its schedules and presence, then deletes itself. Surfaces
 * covered:
 *   - the Rooms card status row (target via Override, the eco tag, the
 *     "Cancel hold" control, and the bottom-row "Manage hold" entry point),
 *   - the Dashboard page-level button + per-hold strip with its cancel,
 *   - the shared HoldModal in its "hold active" (replace/cancel) state.
 *
 * Determinism on the shared stack (same contract as eco-suspend.spec.ts /
 * vacation-mode.spec.ts): the hold is global backend state, so it is created
 * in beforeAll and cleared in afterAll — every other spec's goldens run
 * without it, and the chromium/mobile/dark projects plus the update→verify
 * double pass all start from identical state. Every countdown the hold UI
 * renders ("ends in …") is backend-derived and time-varying, so it is wrapped
 * in <Frozen> and shows the frozen placeholder under the CI build — nothing
 * else on these surfaces varies between passes.
 *
 * Kitchen is the room held here on purpose: it is the seeded idle baseline
 * (no schedule, no presence — see global-setup ROOM_DEFS), so the hold is the
 * only active state its card ever shows, and no other spec captures an
 * individual Kitchen-card golden that clearing the hold could churn.
 */

const API = `${process.env.PLENUM_URL ?? "http://localhost:8099"}/api`;

// Display-unit-aware hold target (#231): POST /api/rooms/{id}/override runs
// the body through `_to_f(value, unit)`, so a hard-coded 72 would mean 72°C
// on the Celsius leg — outside the 40–90°F hold bound. PLENUM_TEMP_UNIT
// mirrors the stack's TEMPERATURE_UNIT (set by the CI matrix / compose
// override — the same variable playwright.config.ts derives golden names
// from). 72°F and 22°C are both ~71.6–72°F stored, matching the rationale in
// global-setup's scheduleTargetTemp().
const UNIT = (process.env.PLENUM_TEMP_UNIT ?? "F") as "F" | "C";
const HOLD_TARGET = UNIT === "C" ? 22 : 72;
// The exact string fmtTemp renders for the stored °F value (1dp).
const HOLD_TARGET_LABEL = UNIT === "C" ? "22.0°C" : "72.0°F";

async function kitchenRoomId(): Promise<string> {
  const rooms: Array<{ id: string; name: string }> = await (await fetch(`${API}/rooms`)).json();
  const kitchen = rooms.find((r) => r.name === "Kitchen");
  if (!kitchen) throw new Error("Kitchen room not seeded — check global-setup");
  return kitchen.id;
}

test.describe.serial("Temporary hold (#576)", () => {
  let roomId: string;

  test.beforeAll(async () => {
    roomId = await kitchenRoomId();
    // 8h preset (the longest the backend allows) so the hold cannot expire
    // mid-suite; respect_eco false = the default "ignores Eco" rendering.
    const res = await fetch(`${API}/rooms/${roomId}/override`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target_temp: HOLD_TARGET, duration_hours: 8, respect_eco: false }),
    });
    // Fail loudly: a golden captured without the hold would silently bake the
    // idle-baseline card instead of the state this spec exists to cover.
    if (!res.ok) {
      throw new Error(`Seeding hold failed: ${res.status} ${await res.text()}`);
    }
  });

  test.afterAll(async () => {
    // Idempotent DELETE — restores Kitchen to the idle baseline the later
    // specs (rooms.spec's full-page golden, schedule-flow) expect.
    if (roomId) {
      await fetch(`${API}/rooms/${roomId}/override`, { method: "DELETE" });
    }
  });

  test("rooms card shows the held state with its eco tag and cancel control", async ({ page }) => {
    await page.goto("/rooms");
    await page.waitForSelector(".loading", {
      state: "detached",
      timeout: 15_000,
    });
    await page.waitForLoadState("networkidle");

    const card = page.locator(".card").filter({ hasText: "Kitchen" }).first();
    // Status row: target + source + the #576 eco tag and cancel control.
    await expect(card).toContainText("via Override");
    await expect(card).toContainText(HOLD_TARGET_LABEL);
    await expect(card).toContainText("ignores Eco");
    await expect(card.getByRole("button", { name: "Cancel hold" })).toBeVisible();
    // The bottom-row entry point flips to manage mode while a hold is live.
    await expect(card.getByRole("button", { name: "Manage hold" })).toBeVisible();

    // Element-scoped shot of just the held card. The "ends in" countdown is
    // frozen under CI; live temp/vents come from the fixed fake sensors.
    await expect(card).toHaveScreenshot("room-hold-card.png");
  });

  test("dashboard lists the hold in a strip with its own cancel", async ({ page }) => {
    await page.goto("/");
    await page.waitForSelector(".loading", {
      state: "detached",
      timeout: 15_000,
    });
    await page.waitForLoadState("networkidle");

    // The page-level button reflects the active hold count.
    await expect(page.getByTestId("dashboard-hold-btn")).toContainText("1 hold active — manage");

    // One strip row per live hold: room, target, frozen countdown, eco tag,
    // and a per-row cancel.
    const strip = page.getByTestId(`dashboard-hold-${roomId}`);
    await expect(strip).toBeVisible();
    await expect(strip).toContainText("Kitchen");
    await expect(strip).toContainText(HOLD_TARGET_LABEL);
    await expect(strip).toContainText("ends in");
    await expect(strip).toContainText("ignores Eco");
    await expect(strip.getByRole("button", { name: "Cancel" })).toBeVisible();

    // Element-scoped shot of just the strip row (the eco-suspend banner
    // pattern) — the zone cards around it are covered by dashboard.spec.
    await expect(strip).toHaveScreenshot("room-hold-dashboard.png");
  });

  test("modal shows the live hold in replace/cancel mode", async ({ page }) => {
    await page.goto("/");
    await page.waitForSelector(".loading", {
      state: "detached",
      timeout: 15_000,
    });
    await page.waitForLoadState("networkidle");

    await page.getByTestId("dashboard-hold-btn").click();
    const modal = page.getByTestId("hold-modal");
    await expect(modal).toBeVisible();

    // The page-level entry point is unscoped — switch to the held room. Its
    // option is suffixed so held rooms are visible from the picker itself.
    await modal.locator("#hold-room").selectOption(roomId);
    await expect(
      modal.locator("#hold-room option", { hasText: "Kitchen — hold active" })
    ).toHaveCount(1);

    // Selecting a held room flips the modal to its replace/cancel state and
    // loads the hold's own target and eco flag into the form.
    await expect(modal).toContainText("Temporary hold active");
    await expect(modal).toContainText(`held at ${HOLD_TARGET_LABEL}`);
    await expect(modal.locator("#hold-allow-eco")).not.toBeChecked();
    await expect(modal.getByTestId("hold-modal-cancel-hold")).toBeVisible();
    await expect(modal.getByTestId("hold-modal-save")).toHaveText("Replace hold");

    await captureModal(page, modal, "room-hold-modal.png");

    // Close without changing anything — this spec must not mutate state
    // beyond its beforeAll/afterAll bracket.
    await modal.getByRole("button", { name: "Close" }).click();
    await expect(modal).not.toBeVisible();
  });
});
