import { expect, test } from "./fixtures";

for (const locale of ["en", "ko"] as const) {
  for (const reducedMotion of ["no-preference", "reduce"] as const) {
    test(`${locale} route scrolling preserves ${reducedMotion} motion preference`, async ({ page, request }, testInfo) => {
      const warnings: string[] = [];
      page.on("console", (message) => {
        if (message.type() === "warning" && message.text().includes("scroll-behavior")) {
          warnings.push(message.text());
        }
      });
      await page.emulateMedia({ reducedMotion });
      await page.setViewportSize({ width: 1280, height: 720 });
      const response = await request.get("http://127.0.0.1:18100/api/work-items");
      expect(response.status()).toBe(200);
      const work = (await response.json()).find((item: { repository: string }) => item.repository === "demo/artifact-preview");
      expect(work).toBeDefined();
      await page.goto(`/${locale}/work-items/${work.id}`);
      await expect(page.getByRole("heading", { name: work.title, exact: true })).toBeVisible();
      await page.evaluate(() => window.scrollTo({ top: document.documentElement.scrollHeight, behavior: "instant" }));
      await expect.poll(() => page.evaluate(() => window.scrollY)).toBeGreaterThan(200);
      const overview = locale === "en" ? "Dashboard" : "대시보드";
      await page.locator(".sidebar nav").getByRole("link", { name: overview, exact: true }).click();
      await expect(page).toHaveURL(new RegExp(`/${locale}$`));
      await expect(page.getByRole("heading", { name: locale === "en" ? "Work dashboard" : "작업 대시보드", exact: true })).toBeVisible();
      await expect.poll(() => page.evaluate(() => window.scrollY)).toBe(0);
      expect(warnings, "Route changes must opt in to Next.js smooth-scroll handling").toEqual([]);
      await expect(page.locator("html")).toHaveAttribute("data-scroll-behavior", "smooth");
      await expect(page.locator("html")).toHaveCSS("scroll-behavior", reducedMotion === "reduce" ? "auto" : "smooth");
      // The router's temporary override must not stick after navigation.
      expect(await page.locator("html").evaluate((element) => element.style.scrollBehavior)).toBe("");
      const screenshot = testInfo.outputPath(`${locale}-${reducedMotion}-route-return.png`);
      await page.screenshot({ path: screenshot });
      await testInfo.attach("Dashboard after route return", { path: screenshot, contentType: "image/png" });
      await page.getByRole("link", { name: new RegExp(work.title) }).click();
      await expect(page).toHaveURL(new RegExp(`/${locale}/work-items/${work.id}$`));
      await expect(page.getByRole("heading", { name: work.title, exact: true })).toBeVisible();
      await page.goBack();
      await expect(page).toHaveURL(new RegExp(`/${locale}$`));
      await expect(page.locator("html")).toHaveCSS("scroll-behavior", reducedMotion === "reduce" ? "auto" : "smooth");
      expect(warnings).toEqual([]);
    });
  }
}
