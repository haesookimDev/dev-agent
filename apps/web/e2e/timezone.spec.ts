import { expect, test } from "./fixtures";

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
