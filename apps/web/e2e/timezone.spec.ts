import { expect, test } from "./fixtures";
import { getMessages } from "../i18n";

test.use({ timezoneId: "Pacific/Honolulu" });

test("work dates use the browser timezone without hydration errors", async ({ page, request }) => {
  const response = await request.post("http://127.0.0.1:18100/api/work-items", { data: {
    title: "Timezone-safe work", repository: "demo/timezone", requirement: "Render work dates across timezones.",
  } });
  expect(response.status()).toBe(201);
  const work = await response.json();
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });
  await page.goto("/en");
  await page.getByRole("searchbox").fill("Timezone-safe work");
  await expect(page.locator(".workRow")).toHaveCount(1);
  const time = page.locator(".workRow time");
  const timestamp = (await time.getAttribute("datetime"))!;
  const utc = /(?:Z|[+-]\d{2}:\d{2})$/i.test(timestamp) ? timestamp : `${timestamp}Z`;
  await expect(time).toHaveText(new Date(utc).toLocaleString("en-US", {
    timeZone: "Pacific/Honolulu", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  }));
  await page.goto(`/en/work-items/${work.id}`);
  await expect(page).toHaveURL(`/en/work-items/${work.id}`);
  await expect(page.locator(".liveDot")).toHaveText("Live");
  expect(errors).toEqual([]);
});

for (const locale of ["ko", "en"] as const) {
  test(`${locale} hydration does not depend on server and browser locale data matching`, async ({ page, request }) => {
    const response = await request.post("http://127.0.0.1:18100/api/work-items", { data: {
      title: `Locale-data-safe work ${locale}`, repository: "demo/locale-data", requirement: "Render consistently across runtime locale versions.",
    } });
    expect(response.status()).toBe(201);
    const work = await response.json();
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });
    // Deliberately make only the browser's locale formatting differ from SSR.
    await page.addInitScript(() => {
      const full = Date.prototype.toLocaleString;
      const time = Date.prototype.toLocaleTimeString;
      Date.prototype.toLocaleString = function (...args: Parameters<typeof full>) { return `Browser date: ${full.apply(this, args)}`; };
      Date.prototype.toLocaleTimeString = function (...args: Parameters<typeof time>) { return `Browser time: ${time.apply(this, args)}`; };
    });
    const messages = getMessages(locale);
    await page.goto(`/${locale}`);
    await page.getByRole("searchbox").fill(work.title);
    await expect(page.locator(".workRow")).toHaveCount(1);
    await expect(page.locator(".workRow time")).toContainText("Browser date:");
    await page.goto(`/${locale}/work-items/${work.id}`);
    await expect(page.locator(".liveDot")).toHaveText(messages.run.live);
    const time = page.locator(".events time").last();
    await expect(time).toContainText("Browser time:");
    const expected = await time.evaluate((element, tag) => new Date(element.getAttribute("datetime")!).toLocaleTimeString(tag), locale === "ko" ? "ko-KR" : "en-US");
    await expect(time).toHaveText(expected);
    expect(errors).toEqual([]);
  });
}
