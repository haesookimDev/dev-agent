import { expect, test } from "./fixtures";

for (const [locale, title, expired, open, original] of [
  ["ko", "검증 자료", "보존 기간 만료", "미리보기", "원본 열기"],
  ["en", "Evidence", "Retention expired", "Preview", "Open original"],
]) {
  test(`${locale} actual CLI-expired evidence keeps metadata without dead actions`, async ({ page, request }, testInfo) => {
    const works = await (await request.get("http://127.0.0.1:18100/api/work-items")).json();
    const work = works.find((row: { repository: string }) => row.repository === "demo/artifact-retention");
    expect(work).toBeDefined();
    if (locale === "ko") await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(`/${locale}/work-items/${work.id}`);
    const evidence = page.getByRole("region", { name: title, exact: true });
    await expect(evidence).toContainText("retained-evidence.txt");
    await expect(evidence).toContainText(expired);
    await expect(evidence.getByRole("button", { name: new RegExp(open) })).toHaveCount(0);
    await expect(evidence.getByRole("link", { name: new RegExp(original) })).toHaveCount(0);
    const artifacts = await (await request.get(`http://127.0.0.1:18100/api/work-items/${work.id}/artifacts`)).json();
    expect(artifacts[0].expired_at).toBeTruthy();
    const response = await request.get(`http://127.0.0.1:18100/api/work-items/${work.id}/artifacts/${artifacts[0].id}`);
    expect(response.status()).toBe(410);
    expect(response.headers()["cache-control"]).toBe("no-store");
    await evidence.scrollIntoViewIfNeeded();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    const screenshot = testInfo.outputPath(`${locale}-retention.png`);
    await page.screenshot({ path: screenshot });
    await testInfo.attach(`${locale} expired evidence`, { path: screenshot, contentType: "image/png" });
  });
}

test("expiration discovered from an open preview removes retry and restores usable focus", async ({ page, request }) => {
  const works = await (await request.get("http://127.0.0.1:18100/api/work-items")).json();
  const work = works.find((row: { repository: string }) => row.repository === "demo/artifact-preview");
  const artifacts = await (await request.get(`http://127.0.0.1:18100/api/work-items/${work.id}/artifacts`)).json();
  const artifact = artifacts.find((row: { name: string }) => row.name === "result.json");
  await page.goto(`/en/work-items/${work.id}`);
  // Only the between-list-and-open timing is injected; the tests above run the actual CLI.
  await page.route(`http://127.0.0.1:18100/api/work-items/${work.id}/artifacts/${artifact.id}`,
    (route) => route.fulfill({ status: 410, contentType: "application/json",
      body: '{"detail":"artifact retention period has expired"}' }));
  await page.getByRole("button", { name: "Preview: result.json", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByRole("alert")).toContainText("retention period has expired");
  await expect(dialog.getByRole("button", { name: "Try again" })).toHaveCount(0);
  await expect(dialog.getByRole("link", { name: /^Open original/ })).toHaveCount(0);
  await expect(dialog).not.toContainText("restore");
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  const evidence = page.getByRole("region", { name: "Evidence", exact: true });
  await expect(evidence.getByRole("heading", { name: "Evidence", exact: true })).toBeFocused();
  await expect(evidence.getByRole("button", { name: "Preview: result.json", exact: true })).toHaveCount(0);
  await expect(evidence.getByRole("link", { name: "Open original: result.json", exact: true })).toHaveCount(0);
  await expect(evidence).toContainText("Retention expired");
});
