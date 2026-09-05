import { expect, test } from "@playwright/test";

test("an unavailable event stream is not presented as live", async ({ page, request }) => {
  const response = await request.post("http://127.0.0.1:18100/api/work-items", { data: {
    title: "Stream reconnection", repository: "demo/stream-test", requirement: "Report unavailable event streams honestly.",
  } });
  expect(response.status()).toBe(201);
  const work = await response.json();
  await page.route(`**/api/work-items/${work.id}/events?*`, (route) => route.abort("failed"));
  await page.goto(`/en/work-items/${work.id}`);
  await expect(page.locator(".liveDot")).toHaveText("Reconnecting");
  await page.unroute(`**/api/work-items/${work.id}/events?*`);
  await expect(page.locator(".liveDot")).toHaveText("Live");
  await expect(page.locator(".runStatus")).toContainText("Awaiting approval");
});
