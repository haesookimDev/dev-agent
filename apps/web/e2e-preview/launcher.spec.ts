import { expect, test, type Page } from "@playwright/test";
import { getMessages, type Locale } from "../i18n";

async function createWork(page: Page, locale: Locale) {
  const messages = getMessages(locale);
  await page.goto(`/${locale}`);
  await page.getByRole("textbox", { name: messages.create.repository, exact: true }).fill("demo/preview-test");
  await page.getByRole("textbox", { name: messages.create.workTitle, exact: true }).fill("Secure Preview acceptance");
  await page.getByRole("textbox", { name: messages.create.requirement, exact: true }).fill("Verify the live Preview without sharing dashboard credentials.");
  await page.getByRole("button", { name: messages.create.submit, exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`/${locale}/work-items/[a-f0-9-]+$`));
  await expect(page.getByRole("button", { name: messages.preview.open, exact: true })).toBeVisible();
  return messages;
}

for (const locale of ["en", "ko"] as const) {
  test(`${locale} accessible launcher, responsive layout and isolated new tab`, async ({ page }) => {
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    const messages = await createWork(page, locale);
    await expect(page.locator(".liveDot")).toHaveText(messages.run.live);
    const button = page.getByRole("button", { name: messages.preview.open, exact: true });
    for (const width of [1440, 390]) {
      await page.setViewportSize({ width, height: 900 });
      await button.focus();
      await page.keyboard.press("Tab");
      await page.keyboard.press("Shift+Tab");
      await expect(button).toBeFocused();
      expect(await button.evaluate((element) => getComputedStyle(element).outlineStyle)).not.toBe("none");
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      if (process.env.KELPIE_PREVIEW_EVIDENCE) {
        await page.screenshot({ path: `${process.env.KELPIE_PREVIEW_EVIDENCE}/${locale}-${width}.png`, fullPage: true });
      }
    }
    const popup = page.waitForEvent("popup");
    await button.press("Enter");
    const preview = await popup;
    preview.on("pageerror", (error) => errors.push(error.message));
    await expect(preview.getByRole("heading", { name: "Isolated Preview", exact: true })).toBeVisible();
    await expect(preview.getByRole("status")).toHaveText("Preview connected");
    expect(await preview.evaluate(() => window.opener === null)).toBe(true);
    await preview.getByRole("button", { name: "Clicked 0 times" }).click();
    await expect(preview.getByRole("button", { name: "Clicked 1 times" })).toBeVisible();
    await expect(page.locator(".previewCard")).toContainText(messages.preview.opened);
    expect(await page.locator('input[name="code"]').count()).toBe(0);
    expect(await page.evaluate(() => [localStorage, sessionStorage].every((storage) => {
      return Object.values(storage).every((value) => !/^kp[al]_/.test(value));
    }))).toBe(true);
    expect(errors).toEqual([]);
  });
}

test("availability, launch errors, popup denial and retry keep the dashboard usable", async ({ page, context }) => {
  const messages = await createWork(page, "en");
  const open = page.getByRole("button", { name: messages.preview.open, exact: true });
  const grantRoute = "**/api/work-items/*/preview-grants";
  await page.route(grantRoute, (route) => route.fulfill({ status: 403, json: { detail: "Forbidden" } }));
  await open.click();
  await expect(page.locator(".previewCard").getByRole("alert")).toHaveText(messages.preview.permissionError);
  await expect.poll(() => context.pages().length).toBe(1);
  await page.unroute(grantRoute);
  await page.evaluate(() => { window.open = () => null; });
  await open.click();
  await expect(page.locator(".previewCard").getByRole("alert")).toHaveText(messages.preview.popupBlocked);
  const accessRoute = "**/api/work-items/*/preview-access";
  await page.route(accessRoute, (route) => route.fulfill({ status: 503, json: {} }));
  await page.reload();
  await expect(page.locator(".previewCard")).toContainText(messages.preview.checkError);
  await page.unroute(accessRoute);
  await page.route(accessRoute, (route) => route.fulfill({ json: { available: false, reason: "unavailable" } }));
  await page.getByRole("button", { name: messages.preview.retry, exact: true }).click();
  await expect(page.locator(".previewCard")).toContainText(messages.preview.unavailable);
  await page.unroute(accessRoute);
  let release!: () => void;
  const pending = new Promise<void>((resolve) => { release = resolve; });
  await page.route(accessRoute, async (route) => { await pending; await route.continue(); });
  await page.getByRole("button", { name: messages.preview.retry, exact: true }).click();
  await expect(page.locator(".previewCard")).toContainText(messages.preview.checking);
  release();
  await expect(open).toBeVisible();
  await page.unroute(accessRoute);
  let finish!: () => void;
  const delayed = new Promise<void>((resolve) => { finish = resolve; });
  await page.route(grantRoute, async (route) => { await delayed; await route.continue(); });
  const popup = page.waitForEvent("popup");
  await open.click();
  await expect(page.getByRole("button", { name: messages.preview.opening, exact: true })).toBeDisabled();
  finish();
  const preview = await popup;
  await expect(preview.getByRole("heading", { name: "Isolated Preview", exact: true })).toBeVisible();
});
