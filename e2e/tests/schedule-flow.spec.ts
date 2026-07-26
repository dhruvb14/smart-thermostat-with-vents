import { test, expect, withExpandedModal } from "./fixtures";

/**
 * Stepped visual coverage of the full scheduling UI flow (Issue #359).
 *
 * The existing schedules.spec.ts only screenshots the collapsed list. This
 * spec drives the create / overlap / validation / enable-disable / expiry /
 * copy flows and snapshots a golden between steps and at each known failure
 * point, in both the °F and °C matrix legs (filenames are unit-suffixed by
 * playwright.config.ts).
 *
 * Determinism on the shared stack: like every mutating flow this resets the
 * rooms it touches in beforeAll/afterAll (via the REST API) so the chromium +
 * mobile projects and the update→verify double pass all start from the same
 * state. It only touches Bedroom / Kitchen / Office — never Living Room, whose
 * seeded block schedules.spec.ts screenshots — and restores them to empty when
 * done, so no other spec's golden is affected.
 */

const API = `${process.env.PLENUM_URL ?? "http://localhost:8099"}/api`;
const UNIT = (process.env.PLENUM_TEMP_UNIT ?? "F") as "F" | "C";
// A target comfortable and in-range in both units (≈68°F / 20°C).
const TARGET = UNIT === "C" ? "20" : "68";
// A fixed, far-future expiry so the rendered "Expires" cell is deterministic.
const EXPIRY = "2030-06-01T08:00";
// A drift band inside the 0–10 °F bound in both units (1.8 °F ≡ 1 °C).
const DRIFT = UNIT === "C" ? "1" : "1.8";

const SOURCE_ROOM = "Bedroom";
const COPY_TARGETS = ["Kitchen", "Office"];
const TOUCHED = [SOURCE_ROOM, ...COPY_TARGETS];

interface ApiRoom {
  id: string;
  name: string;
}
interface ApiSchedule {
  id: string;
}

async function roomsByName(): Promise<Record<string, string>> {
  const rooms: ApiRoom[] = await (await fetch(`${API}/rooms`)).json();
  return Object.fromEntries(rooms.map((r) => [r.name, r.id]));
}

async function clearSchedules(roomId: string): Promise<void> {
  const scheds: ApiSchedule[] = await (await fetch(`${API}/rooms/${roomId}/schedules`)).json();
  for (const s of scheds) {
    await fetch(`${API}/rooms/${roomId}/schedules/${s.id}`, { method: "DELETE" });
  }
}

async function addSchedule(roomId: string, body: Record<string, unknown>): Promise<void> {
  await fetch(`${API}/rooms/${roomId}/schedules`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

async function resetRooms(): Promise<void> {
  const map = await roomsByName();
  for (const name of TOUCHED) {
    if (map[name]) await clearSchedules(map[name]);
  }
}

test.describe.serial("Scheduling flow (#359)", () => {
  test.beforeAll(resetRooms);
  test.afterAll(resetRooms);

  test("scheduling UI flow", async ({ page }) => {
    await page.goto("/schedules");
    await page.waitForSelector(".loading", { state: "detached", timeout: 15_000 });
    await page.waitForLoadState("networkidle");

    const card = () => page.locator(".card").filter({ hasText: SOURCE_ROOM }).first();
    const modal = page.locator(".modal");

    // The schedule editor is taller than the mobile viewport, so each capture of
    // it is wrapped to expand it — otherwise the golden clips to 90vh and loses
    // the title, day picker and any error banner off the top. Card captures are
    // deliberately NOT wrapped: they need the untouched layout, or the sticky
    // nav overlaps them. Nor is the copy modal, which is short enough to fit.
    const modalShot = (name: string) =>
      withExpandedModal(page, () => expect(modal).toHaveScreenshot(name));

    // ── Step 1: empty state ────────────────────────────────────────────────
    await card().getByText(SOURCE_ROOM).click();
    await expect(card().getByText("No schedules. Add one below.")).toBeVisible();
    await expect(card()).toHaveScreenshot("schedule-empty.png");

    // ── Step 2: create modal (default "Never expire") ──────────────────────
    await card().getByText("+ Add schedule block").click();
    await modal.waitFor({ state: "visible" });
    await modal.getByLabel(/Target temperature/i).fill(TARGET);
    await modalShot("schedule-create-modal.png");

    // ── Step 2a/2b: Temperature drift shows/hides the band input (#517) ─────
    // The whole point of the control is that the number field only exists in
    // "custom" mode, so both states get a golden. The visibility assertions
    // are deliberate duplicates of what the screenshots show — a golden that
    // gets silently regenerated proves nothing; these fail loudly.
    const driftInherit = modal.getByRole("radio", { name: /normal deadband/i });
    const driftCustom = modal.getByRole("radio", { name: /just for this block/i });
    const bandInput = modal.getByLabel(/^Deadband/i);

    await expect(driftInherit).toBeChecked();
    await expect(bandInput).toHaveCount(0);
    await modalShot("schedule-drift-inherit.png");

    await driftCustom.click();
    await expect(bandInput).toBeVisible();
    // Typed, never derived — a clock- or engine-derived value would render
    // differently between the update pass and the verify pass.
    await bandInput.fill(DRIFT);
    await modalShot("schedule-drift-custom.png");

    // Back to inherit so the block created in step 4 carries no band and every
    // downstream golden in this flow is unaffected by these two steps.
    await driftInherit.click();
    await expect(bandInput).toHaveCount(0);

    // ── Step 3: "Auto-disable at" picker visible ───────────────────────────
    await modal.getByLabel("Auto-disable at").click();
    await modal.getByLabel("Auto-disable date and time").fill(EXPIRY);
    await modalShot("schedule-expiry-picker.png");

    // ── Step 4: after successful create (row shows target + expiry) ─────────
    await modal.getByRole("button", { name: /^Save$/ }).click();
    await modal.waitFor({ state: "detached" });
    await expect(card().locator("tr").filter({ hasText: "22:00" })).toBeVisible();
    await expect(card()).toHaveScreenshot("schedule-created.png");

    // ── Step 5: overlap rejection (default 22:00–07:00 collides) ───────────
    await card().getByText("+ Add schedule block").click();
    await modal.waitFor({ state: "visible" });
    await modal.getByLabel(/Target temperature/i).fill(TARGET);
    await modal.getByRole("button", { name: /^Save$/ }).click();
    await expect(modal.getByText(/Overlaps with existing block/)).toBeVisible();
    await modalShot("schedule-overlap-error.png");

    // ── Step 6: validation error (out-of-range target) ─────────────────────
    await modal.getByLabel(/Target temperature/i).fill("999");
    await modal.getByRole("button", { name: /^Save$/ }).click();
    await expect(modal.getByText(/Target temperature must be between/)).toBeVisible();
    await modalShot("schedule-validation-error.png");
    await modal.getByRole("button", { name: /Cancel/ }).click();
    await modal.waitFor({ state: "detached" });

    // ── Step 7: disabled (parked) state + active/inactive badges ───────────
    const row22 = card().locator("tr").filter({ hasText: "22:00" });
    await row22.getByRole("button", { name: "Disable" }).click();
    await expect(card().getByText("0 active")).toBeVisible();
    await expect(card().getByText("1 inactive")).toBeVisible();
    await expect(card()).toHaveScreenshot("schedule-disabled.png");

    // ── Step 8: re-enable conflict (slot reused while parked) ───────────────
    // Add an overlapping enabled block while the original is parked, then try
    // to re-enable the original → backend rejects.
    await card().getByText("+ Add schedule block").click();
    await modal.waitFor({ state: "visible" });
    await modal.getByLabel(/Start time/i).fill("23:00");
    await modal.getByLabel(/End time/i).fill("06:00");
    await modal.getByLabel(/Target temperature/i).fill(TARGET);
    await modal.getByRole("button", { name: /^Save$/ }).click();
    await modal.waitFor({ state: "detached" });
    await card()
      .locator("tr")
      .filter({ hasText: "22:00" })
      .getByRole("button", { name: "Enable" })
      .click();
    await expect(card().getByText(/Overlaps with existing block/)).toBeVisible();
    await expect(card()).toHaveScreenshot("schedule-reenable-conflict.png");

    // ── Step 9: copy modal (multi-room select) ─────────────────────────────
    // Pre-seed Kitchen with an overlapping block so one target conflicts.
    const map = await roomsByName();
    await addSchedule(map[COPY_TARGETS[0]], {
      days_of_week: [0, 1, 2, 3, 4],
      start_time: "23:00",
      end_time: "06:00",
      target_temp: Number(TARGET),
    });
    // Copy the enabled 23:00 block.
    await card()
      .locator("tr")
      .filter({ hasText: "23:00" })
      .getByRole("button", { name: "Copy" })
      .click();
    await modal.waitFor({ state: "visible" });
    await modal.getByLabel(COPY_TARGETS[0]).check();
    await modal.getByLabel(COPY_TARGETS[1]).check();
    await expect(modal).toHaveScreenshot("schedule-copy-modal.png");

    // ── Step 10: copy results (one created, one created-disabled-conflict) ──
    await modal.getByRole("button", { name: /^Copy$/ }).click();
    await expect(card().getByText("Copy results")).toBeVisible();
    await expect(card().getByText(/Copied \(disabled\)/)).toBeVisible();
    await expect(card()).toHaveScreenshot("schedule-copy-results.png");
  });
});
