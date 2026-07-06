import { test, expect } from "./fixtures";

test("room settings — full page with all room controls", async ({ page }) => {
  await page.goto("/rooms");
  await page.waitForSelector(".loading", { state: "detached", timeout: 15_000 });
  await page.waitForLoadState("networkidle");

  // Open the Living Room's settings. Scope to the room card — the bare
  // getByRole("button", { name: /Settings/i }) also matches the gear in the top
  // nav (aria-label="Settings"), which is the wrong target.
  await page
    .locator(".card")
    .filter({ hasText: "Living Room" })
    .first()
    .getByRole("button", { name: "Settings", exact: true })
    .click();

  // Room settings used to be a tall modal that the visual suite could only
  // capture partially (the dialog overflowed the viewport and scrolled). It now
  // renders as its own full page — like "Configure sensors & vents".
  const settings = page.getByTestId("room-settings");
  await settings.waitFor({ state: "visible" });
  // The Eco worked-example resolves from the selected thermostat's config; wait
  // for the page to settle so the golden captures a stable render.
  await page.waitForLoadState("networkidle");

  // Grow the viewport tall enough to paint the whole form in a single pass.
  // Otherwise Playwright stitches the capture in viewport-height slices, and
  // because it scrolls between slices the position:sticky <nav> re-renders at
  // each slice top — splicing the "Plenum" bar into the middle of the page on
  // high-DPI mobile and clipping the title under it on desktop. One paint (no
  // scroll) avoids both; width/DPI stay per-project so the layout is unchanged,
  // and the element capture sits below the sticky nav so the nav is excluded.
  const vp = page.viewportSize();
  if (vp) await page.setViewportSize({ width: vp.width, height: 4000 });

  // Screenshot the settings container element (not fullPage): it captures the
  // full form — presence, offset, deadband, pre-cool/pre-heat, and the Eco Mode
  // override block with its worked example (#404) — in one clean image (#182).
  await expect(settings).toHaveScreenshot("room-settings.png");
});
