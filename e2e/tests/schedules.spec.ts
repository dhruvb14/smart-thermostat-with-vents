import { test, expect } from "./fixtures";

const API = `${process.env.PLENUM_URL ?? "http://localhost:8099"}/api`;

test("schedules list", async ({ page }) => {
  await page.goto("/schedules");
  await page.waitForSelector(".loading", { state: "detached", timeout: 15_000 });
  await page.waitForLoadState("networkidle");

  await expect(page).toHaveScreenshot("schedules.png", { fullPage: true });
});

test("schedule delete confirmation dialog", async ({ page }) => {
  await page.goto("/schedules");
  await page.waitForSelector(".loading", { state: "detached", timeout: 15_000 });
  await page.waitForLoadState("networkidle");

  // Expand a room to reveal its schedule rows and the "Del" button.
  await page.getByText("Living Room").first().click();
  await page.getByRole("button", { name: "Del" }).first().click();
  const dialog = page.getByTestId("confirm-dialog");
  await expect(dialog).toBeVisible();
  // Scope to the message <p>, not the whole dialog — matches the pattern
  // used for the other confirm-dialog specs (see rooms.spec.ts).
  await expect(dialog.locator("p")).toContainText("Delete this schedule");

  await expect(page).toHaveScreenshot("schedule-delete-confirm.png", { maxDiffPixels: 800 });

  // Cancel — don't actually delete a schedule other specs depend on.
  await dialog.getByRole("button", { name: "Cancel" }).click();
});

/**
 * Named blocks, and the block id that stays reachable beside them (Issue #520).
 *
 * Living Room's two seeded blocks are the only golden-visible ones no spec
 * mutates (see global-setup.ts), so this is where the populated Name column
 * gets a stable golden in both unit legs.
 *
 * The id assertion is deliberately NOT a screenshot: a schedule id is a GUID
 * minted per stack, so rendering it as text would make this golden churn on
 * every CI run. It lives in the ID chip's `title` — a native tooltip, which is
 * OS-drawn and never captured — and is checked here against what the API
 * actually returns, which is the value someone needs to address this block from
 * REST/MCP (and, per #519, an MQTT command topic).
 */
test("named schedule blocks keep their id reachable (#520)", async ({ page }) => {
  interface ApiRoom {
    id: string;
    name: string;
  }
  interface ApiSchedule {
    id: string;
    name: string | null;
    start_time: string;
  }

  const rooms: ApiRoom[] = await (await fetch(`${API}/rooms`)).json();
  const living = rooms.find((r) => r.name === "Living Room");
  expect(living, "Living Room should be seeded by global-setup").toBeTruthy();
  const blocks: ApiSchedule[] = await (await fetch(`${API}/rooms/${living!.id}/schedules`)).json();
  // The API orders by start_time, and so does the table — so row 1 is 08:00.
  const first = blocks[0];
  expect(first.name).toBe("Daytime comfort");

  await page.goto("/schedules");
  await page.waitForSelector(".loading", { state: "detached", timeout: 15_000 });
  await page.waitForLoadState("networkidle");

  const card = page.locator(".card").filter({ hasText: "Living Room" }).first();
  await card.getByText("Living Room").first().click();

  // The name the user gave the block is what the list shows…
  await expect(card.getByText("Daytime comfort")).toBeVisible();
  await expect(card.getByText("Evening wind-down")).toBeVisible();
  // …and the id is still one hover away, without dev tools or a DB query.
  await expect(card.getByTestId("schedule-id").first()).toHaveAttribute(
    "title",
    `Schedule ID: ${first.id}`
  );

  await expect(card).toHaveScreenshot("schedules-named.png");

  // Tap-to-reveal, checked AFTER the screenshot on purpose: a revealed GUID in
  // the capture would churn this golden on every CI run. It exists because a
  // `title` tooltip never opens on a touch screen, so hover alone would put the
  // id out of reach on a phone.
  const chip = card.getByTestId("schedule-id").first();
  await expect(chip).toHaveAttribute("aria-expanded", "false");
  await chip.click();
  await expect(card.getByText(first.id)).toBeVisible();
});
