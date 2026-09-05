import { expect, test } from "@playwright/test";

test("OIDC browser exchange, real WebSocket and logout revocation", async ({ page, context }) => {
  await page.goto("/en");
  await expect(page.getByRole("heading", { name: "Work dashboard" })).toBeVisible();
  await page.getByRole("textbox", { name: "Repository", exact: true }).fill("demo/preview-test");
  await page.getByRole("textbox", { name: "Title", exact: true }).fill("Secure Preview acceptance");
  await page.getByRole("textbox", { name: "Requirement", exact: true }).fill("Verify the live Preview without sharing dashboard credentials.");
  await page.getByRole("button", { name: "Start development", exact: true }).click();
  await expect(page).toHaveURL(/\/en\/work-items\/[a-f0-9-]+$/);
  const workId = page.url().split("/").at(-1)!;
  await expect.poll(() => page.evaluate(async (id) => {
    const response = await fetch(`https://localhost:18443/api/work-items/${id}/preview-access`, { credentials: "include" });
    return (await response.json()).available;
  }, workId)).toBe(true);
  if (process.env.KELPIE_PREVIEW_BEFORE) {
    await page.screenshot({ path: process.env.KELPIE_PREVIEW_BEFORE, fullPage: true });
  }
  // Protocol-level navigation independent of the dashboard launcher implementation.
  await page.evaluate(async (id) => {
    const response = await fetch(`https://localhost:18443/api/work-items/${id}/preview-grants`, {
      method: "POST", credentials: "include",
    });
    if (response.status !== 201) throw new Error("Preview grant failed");
    const grant = await response.json();
    const form = document.createElement("form");
    form.action = grant.exchange_url; form.method = "POST";
    const code = document.createElement("input");
    code.type = "hidden"; code.name = "code"; code.value = grant.launch_code;
    form.append(code); document.body.append(form); form.submit();
  }, workId);
  await expect(page.getByRole("heading", { name: "Isolated Preview", exact: true })).toBeVisible();
  await expect(page.getByRole("status")).toHaveText("Preview connected");
  expect(new URL(page.url()).search).toBe("");
  expect(new URL(page.url()).hostname).toBe(`${workId}.preview.localhost`);
  const cookie = (await context.cookies(page.url())).find((value) => value.name === "__Host-kelpie_preview")!;
  // Assert properties, never print the credential value in test output.
  expect(!!cookie).toBe(true);
  expect(cookie.secure && cookie.httpOnly && cookie.sameSite === "Strict").toBe(true);
  expect(cookie.domain === `${workId}.preview.localhost` && cookie.path === "/").toBe(true);
  expect(await page.evaluate(() => document.cookie.includes("kelpie"))).toBe(false);
  await page.getByRole("button", { name: "Clicked 0 times" }).click();
  await expect(page.getByRole("button", { name: "Clicked 1 times" })).toBeVisible();
  expect(await page.evaluate(async () => (await fetch("/headers")).json())).toEqual({
    has_authorization: false, has_platform_cookie: false, has_work_scope: true,
  });
  expect(await page.evaluate(async () => (await fetch("/console")).status)).toBe(503);
  const dashboard = await context.newPage();
  await dashboard.goto("/en");
  expect(await dashboard.evaluate(async () => (await fetch("https://localhost:18443/auth/logout", {
    method: "POST", credentials: "include",
  })).status)).toBe(204);
  await expect(page.getByRole("status")).toHaveText("Preview disconnected", { timeout: 6000 });
  expect((await page.reload())?.status()).toBe(401);
  await expect(page.locator("body")).toContainText("dashboard");
});
