import { expect, test } from "@playwright/test";
import { PNG } from "pngjs";

function nonBackgroundPixelRatio(buffer: Buffer): number {
  const png = PNG.sync.read(buffer);
  let nonBackground = 0;
  const total = png.width * png.height;
  for (let index = 0; index < png.data.length; index += 4) {
    const r = png.data[index];
    const g = png.data[index + 1];
    const b = png.data[index + 2];
    const a = png.data[index + 3];
    if (a > 0 && !(r > 210 && g > 220 && b > 230)) nonBackground += 1;
  }
  return nonBackground / total;
}

test("renders a nonblank 3D factory scene", async ({ page }) => {
  const logPath = process.env.MANSIM_3D_TEST_LOG;
  await page.goto(logPath ? `/?${new URLSearchParams({ log: logPath }).toString()}` : "/");
  await expect(page.getByText("Replay Studio 3D")).toBeVisible();
  await expect(page.locator(".first-person-pip")).toBeVisible();
  await expect(page.locator(".scene-viewport canvas").first()).toBeVisible();
  await expect(page.locator(".first-person-viewport canvas")).toHaveCount(1);
  await page.waitForTimeout(1200);
  const screenshot = await page.screenshot();
  expect(nonBackgroundPixelRatio(screenshot)).toBeGreaterThan(0.02);
  await expect(page.getByRole("button", { name: /Worker/i })).toBeVisible();
});
