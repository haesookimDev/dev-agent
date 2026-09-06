import { readFile } from "node:fs/promises";
import type { APIRequestContext, Page } from "@playwright/test";
import { expect, test } from "./fixtures";

async function openEvidence(page: Page, request: APIRequestContext, locale = "en") {
  const response = await request.get("http://127.0.0.1:18100/api/work-items");
  expect(response.status()).toBe(200);
  const work = (await response.json()).find((item: { repository: string }) => item.repository === "demo/artifact-preview");
  expect(work).toBeDefined();
  const artifacts = await (await request.get(`http://127.0.0.1:18100/api/work-items/${work.id}/artifacts`)).json();
  await page.goto(`/${locale}/work-items/${work.id}`);
  return (name: string) => `http://127.0.0.1:18100/api/work-items/${work.id}/artifacts/${artifacts.find((item: { name: string }) => item.name === name).id}`;
}

test("real uploaded text, JSON and image preview preserve bytes, modal focus and original download", async ({ page, request }) => {
  const artifact = await openEvidence(page, request);
  const original = await (await request.get(artifact("검증 결과 ✅.txt"))).body();
  const trigger = page.getByRole("button", { name: "Preview: 검증 결과 ✅.txt", exact: true });
  await trigger.focus();
  await page.keyboard.press("Enter");
  const dialog = page.getByRole("dialog", { name: "검증 결과 ✅.txt", exact: true });
  await expect(dialog).toBeVisible();
  await expect(dialog.locator("pre")).toHaveText(original.toString("utf8"));
  expect(await page.evaluate(() => document.documentElement.dataset.artifactProbe)).toBeUndefined();
  await expect(dialog.locator("script, iframe, object, embed")).toHaveCount(0);
  await expect(dialog.getByRole("button", { name: /^Close/ })).toBeFocused();
  await page.keyboard.press("Shift+Tab");
  await expect(dialog.getByRole("link", { name: /^Open original/ })).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(dialog.getByRole("button", { name: /^Close/ })).toBeFocused();
  const downloadEvent = page.waitForEvent("download");
  await dialog.getByRole("link", { name: /^Open original/ }).click({ modifiers: ["Alt"] });
  const download = await downloadEvent;
  expect(download.suggestedFilename()).toBe("검증 결과 ✅.txt");
  expect(await readFile((await download.path())!)).toEqual(original);
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(trigger).toBeFocused();
  await page.getByRole("button", { name: "Preview: result.json", exact: true }).click();
  await expect(page.getByRole("dialog").locator("pre")).toHaveText('{"result":"synthetic evidence"}');
  await page.getByRole("button", { name: /^Close/ }).click();
  await page.getByRole("button", { name: "Preview: evidence.png", exact: true }).click();
  const image = page.getByRole("img", { name: "Evidence image: evidence.png" });
  await expect(image).toBeVisible();
  expect(await image.evaluate((element: HTMLImageElement) => [element.naturalWidth, element.naturalHeight])).toEqual([32, 32]);
  expect(await image.getAttribute("src")).toMatch(/^blob:/);
  await page.keyboard.press("Escape");
});

test("Korean narrow dark-preference preview stays readable and missing evidence has actionable retry", async ({ page, request }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.emulateMedia({ colorScheme: "dark" });
  const artifact = await openEvidence(page, request, "ko");
  await page.getByRole("button", { name: "미리보기: 검증 결과 ✅.txt", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.locator("pre")).toContainText("Synthetic probe not run");
  expect(await dialog.evaluate((element) => {
    const box = element.getBoundingClientRect();
    return box.left >= 0 && box.right <= innerWidth && box.top >= 0 && box.bottom <= innerHeight;
  })).toBe(true);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  expect(await dialog.evaluate((element) => getComputedStyle(element).colorScheme)).toBe("light");
  const screenshot = testInfo.outputPath("ko-mobile.png");
  await page.screenshot({ path: screenshot });
  await testInfo.attach("Korean narrow preview", { path: screenshot, contentType: "image/png" });
  await page.keyboard.press("Escape");
  const missing = page.waitForResponse(artifact("missing.txt"));
  await page.getByRole("button", { name: "미리보기: missing.txt", exact: true }).click();
  expect((await missing).status()).toBe(410);
  await expect(dialog.getByRole("alert")).toContainText("복구");
  const retried = page.waitForResponse(artifact("missing.txt"));
  await dialog.getByRole("button", { name: "다시 시도" }).click();
  expect((await retried).status()).toBe(410);
  await expect(dialog.getByRole("alert")).toContainText("복구");
  await page.keyboard.press("Escape");
  await expect(page.getByRole("button", { name: "미리보기: missing.txt", exact: true })).toBeFocused();
});

test("injected slow/error responses are cancellable and retry never shows a previous file", async ({ page, request }) => {
  const artifact = await openEvidence(page, request);
  // Only transport-failure cases are injected; the success journey above uses actual uploads.
  let finishSlow!: () => void;
  const held = new Promise<void>((resolve) => { finishSlow = resolve; });
  await page.route(artifact("검증 결과 ✅.txt"), async (route) => {
    await held;
    await route.fulfill({ contentType: "text/plain", body: "Stale response must not replace JSON" });
  });
  await page.getByRole("button", { name: "Preview: 검증 결과 ✅.txt", exact: true }).click();
  await expect(page.getByRole("dialog").getByRole("status")).toHaveText("Reading the file…");
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "Preview: result.json", exact: true }).click();
  finishSlow();
  await expect(page.getByRole("dialog").locator("pre")).toHaveText('{"result":"synthetic evidence"}');
  await page.keyboard.press("Escape");
  let denied = true;
  await page.route(artifact("result.json"), async (route) => {
    if (denied) await route.fulfill({ status: 403, body: "Sensitive diagnostic must not be displayed" });
    else await route.continue();
  });
  await page.getByRole("button", { name: "Preview: result.json", exact: true }).click();
  await expect(page.getByRole("dialog").getByRole("alert")).toContainText("access");
  await expect(page.getByRole("dialog")).not.toContainText("Sensitive diagnostic");
  denied = false;
  await page.getByRole("button", { name: "Try again", exact: true }).click();
  await expect(page.getByRole("dialog").locator("pre")).toHaveText('{"result":"synthetic evidence"}');
  await page.keyboard.press("Escape");
});

test("image object URLs are released on close and invalid image retry uses a new request", async ({ page, request }) => {
  const artifact = await openEvidence(page, request);
  await page.evaluate(() => {
    const create = URL.createObjectURL.bind(URL);
    const revoke = URL.revokeObjectURL.bind(URL);
    const active = new Set<string>();
    URL.createObjectURL = (blob) => { const url = create(blob); active.add(url); return url; };
    URL.revokeObjectURL = (url) => { active.delete(url); revoke(url); };
    Object.defineProperty(window, "activePreviewImages", { get: () => active.size });
  });
  const trigger = page.getByRole("button", { name: "Preview: evidence.png", exact: true });
  const image = page.getByRole("img", { name: "Evidence image: evidence.png" });
  await trigger.click();
  await expect(image).toBeVisible();
  expect(await page.evaluate(() => Reflect.get(window, "activePreviewImages"))).toBe(1);
  await page.keyboard.press("Escape");
  expect(await page.evaluate(() => Reflect.get(window, "activePreviewImages"))).toBe(0);
  let invalid = true;
  await page.route(artifact("evidence.png"), (route) => invalid ?
    route.fulfill({ contentType: "image/png", body: "Not a PNG" }) : route.continue());
  await trigger.click();
  await expect(page.getByRole("dialog").getByRole("alert")).toContainText("valid");
  expect(await page.evaluate(() => Reflect.get(window, "activePreviewImages"))).toBe(0);
  invalid = false;
  await page.getByRole("button", { name: "Try again", exact: true }).click();
  await expect(image).toBeVisible();
  await expect.poll(() => image.evaluate((element: HTMLImageElement) => element.naturalWidth)).toBe(32);
  await page.keyboard.press("Escape");
  expect(await page.evaluate(() => Reflect.get(window, "activePreviewImages"))).toBe(0);
});

test("injected empty and oversized files, authentication errors and unsafe types have distinct UI states", async ({ page, request }) => {
  const artifact = await openEvidence(page, request);
  const url = artifact("result.json");
  const trigger = page.getByRole("button", { name: "Preview: result.json", exact: true });
  const cases = [
    { status: 401, expected: "sign in" }, { status: 404, expected: "list" },
    { status: 500, expected: "temporarily unavailable" }, { contentType: "text/html", expected: "format" },
    { contentType: "text/plain", headers: { "Content-Length": String(10 * 1024 * 1024 + 1) }, expected: "10 MiB" },
  ];
  for (const { expected, ...response } of cases) {
    await page.route(url, (route) => route.fulfill({ body: "Not user-facing diagnostic", ...response }));
    await trigger.click();
    await expect(page.getByRole("dialog").getByRole("alert")).toContainText(expected);
    await expect(page.getByRole("dialog")).not.toContainText("Not user-facing diagnostic");
    await page.keyboard.press("Escape");
    await page.unroute(url);
  }
  await page.route(url, (route) => route.fulfill({ contentType: "text/plain", body: "" }));
  await trigger.click();
  await expect(page.getByRole("dialog").getByRole("status")).toContainText("empty");
  await page.keyboard.press("Escape");
});
