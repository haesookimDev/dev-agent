import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { getMessages } from "../i18n";
import type { WorkItem } from "../lib/types";
import { expect, test as base } from "./fixtures";

const api = "http://127.0.0.1:18100";
type BudgetWork = Pick<WorkItem, "id" | "status" | "version" | "budget_minutes">;

function worker(action: "create" | "exhaust" | "finish", id?: string): BudgetWork {
  const metadata = JSON.parse(readFileSync(process.env.KELPIE_E2E_BUDGET_METADATA!, "utf8")).metadata as string;
  const output = execFileSync(process.env.KELPIE_E2E_PYTHON || resolve("../../.venv/bin/python"),
    ["e2e/budget-runtime.py", metadata, action, ...(id ? [id] : [])], { encoding: "utf8", timeout: 20_000 });
  return JSON.parse(output);
}

const test = base.extend<{ budgetWork: BudgetWork }>({
  budgetWork: async ({ request, releaseWork }, provideFixture) => {
    // Explicit dependency: finish our scoped lease before the shared cleanup verifies all leases.
    void releaseWork;
    const blocker = await request.post(`${api}/api/work-items`, { data: {
      title: "Occupy the Mock slot during budget acceptance", repository: "demo/budget-approval",
      requirement: "Keep the only Mock execution slot busy so the owned scoped fixture claims its own work.",
    } });
    expect(blocker.status()).toBe(201);
    const blockerId = (await blocker.json()).id;
    await expect.poll(async () => (await (await request.get(`${api}/api/work-items/${blockerId}`)).json()).status).toBe("awaiting_approval");
    const work = worker("create");
    try { await provideFixture(work); } finally { worker("finish", work.id); }
  },
});

for (const locale of ["en", "ko"] as const) {
  test(`${locale}: review and approve additional time with a real API`, async ({ page, request, budgetWork: work }, info) => {
    const messages = getMessages(locale);
    const url = `${api}/api/work-items/${work.id}`;
    await page.setViewportSize(locale === "ko" ? { width: 390, height: 844 } : { width: 1280, height: 900 });
    await page.goto(`/${locale}/work-items/${work.id}`);
    await expect(page.locator(".budgetWork")).toContainText(messages.run.budgetHint);
    const trigger = page.getByRole("button", { name: messages.run.budgetOpen, exact: true });
    await expect(trigger).toHaveCSS("border-radius", "7px");
    await trigger.focus();
    await page.keyboard.press("Enter");
    const dialog = page.getByRole("dialog", { name: messages.run.budgetConfirmTitle });
    const back = dialog.getByRole("button", { name: messages.run.budgetBack, exact: true });
    await expect(back).toBeFocused();
    await page.keyboard.press("Escape");
    await expect(dialog).not.toBeVisible();
    await expect(trigger).toBeFocused();
    expect(await (await request.get(`${url}/audit-log`)).json()).toEqual([]);
    await trigger.click();
    await expect(dialog).toContainText(messages.run.budgetConfirmDescription);
    const input = dialog.getByRole("spinbutton", { name: messages.run.budgetAdditional, exact: true });
    const confirm = dialog.getByRole("button", { name: messages.run.budgetConfirm, exact: true });
    for (const invalid of ["", "14", "1441", "15.5"]) {
      await input.fill(invalid);
      await expect(confirm).toBeDisabled();
      await expect(input).toHaveAttribute("aria-invalid", "true");
      await expect(dialog.getByRole("alert")).toHaveText(messages.run.budgetInvalid);
    }
    await input.fill("1440");
    await expect(confirm).toBeEnabled();
    await input.fill("15");
    await expect(dialog.locator(".budgetSummary dd")).toHaveText([`30 ${messages.run.minuteUnit}`, `45 ${messages.run.minuteUnit}`]);
    await expect(dialog.getByRole("alert")).toHaveCount(0);
    expect(await dialog.evaluate((element) => element.scrollWidth <= element.clientWidth)).toBe(true);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: info.outputPath(`budget-${locale}-confirmation.png`) });
    await page.setViewportSize({ width: 320, height: 568 });
    await confirm.scrollIntoViewIfNeeded();
    expect(await dialog.evaluate((element) => element.scrollWidth <= element.clientWidth)).toBe(true);
    const confirmationBox = await confirm.boundingBox();
    expect(confirmationBox).not.toBeNull();
    expect(confirmationBox!.height).toBeGreaterThanOrEqual(44);
    expect(confirmationBox!.y + confirmationBox!.height).toBeLessThanOrEqual(568);
    await page.evaluate(() => {
      const element = document.querySelector<HTMLDialogElement>("dialog.budgetDialog")!;
      const close = element.close.bind(element);
      element.close = () => {
        close();
        queueMicrotask(() => { document.documentElement.dataset.budgetFocusRestored = String(
          document.activeElement === document.querySelector(".runStatus"),
        ); });
      };
    });
    const response = page.waitForResponse((result) => result.url() === `${url}/approvals` && result.request().method() === "POST");
    await confirm.click();
    const approved = await response;
    expect(approved.status()).toBe(200);
    expect(approved.request().postDataJSON()).toEqual({ kind: "budget", decision: "approve", expected_version: work.version, payload: { minutes: 15 } });
    await expect(dialog).not.toBeVisible();
    await expect(page.locator(".runStatus")).toContainText(messages.status.implementing);
    await expect(page.locator(".runStatus")).toBeFocused();
    await expect(page.locator("html")).toHaveAttribute("data-budget-focus-restored", "true");
    await expect(page.locator(".actionNotice")).toHaveText(messages.run.budgetSaved);
    await expect(trigger).toHaveCount(0);
    const current = await (await request.get(url)).json();
    expect([current.version, current.budget_minutes]).toEqual([work.version + 1, 45]);
    const audit = await (await request.get(`${url}/audit-log`)).json();
    expect(audit).toHaveLength(1);
    expect(audit[0].action).toBe("approval.decided");
    await page.reload();
    await expect(page.locator(".runStatus")).toContainText(messages.status.implementing);
    await expect(trigger).toHaveCount(0);
  });
}

test("budget failures preserve input, block duplicate submissions and allow explicit retry", async ({ page, request, budgetWork: work }) => {
  const messages = getMessages("en");
  const url = `${api}/api/work-items/${work.id}`;
  await page.goto(`/en/work-items/${work.id}`);
  await page.getByRole("button", { name: messages.run.budgetOpen, exact: true }).click();
  const dialog = page.getByRole("dialog");
  const input = dialog.getByRole("spinbutton");
  const confirm = dialog.getByRole("button", { name: messages.run.budgetConfirm, exact: true });
  await input.fill("20");
  let calls = 0;
  let release: () => void = () => {};
  const held = new Promise<void>((resolve) => { release = resolve; });
  await page.route(`${url}/approvals`, async (route) => { calls++; await held; await route.abort("failed"); });
  try {
    await confirm.click();
    await expect(dialog.getByRole("button", { name: messages.run.budgetSaving, exact: true })).toBeDisabled();
    await expect(input).toBeDisabled();
    await page.keyboard.press("Enter");
    await page.keyboard.press("Escape");
    await expect(dialog).toBeVisible();
    expect(calls).toBe(1);
  } finally { release(); }
  await expect(dialog.getByRole("alert")).toHaveText(messages.run.budgetNetworkError);
  await expect(input).toHaveValue("20");
  await expect(confirm).toBeEnabled();
  await page.unroute(`${url}/approvals`);
  // Explicit denial UI fixture; actual OIDC Approver/admin enforcement stays in the API suite.
  await page.route(`${url}/approvals`, (route) => route.fulfill({ status: 403, json: { detail: "approver role required" } }));
  await confirm.click();
  await expect(dialog.getByRole("alert")).toHaveText(messages.run.budgetPermissionError);
  expect(await (await request.get(`${url}/audit-log`)).json()).toEqual([]);
  expect((await (await request.get(url)).json()).budget_minutes).toBe(30);
  await page.unroute(`${url}/approvals`);
  await confirm.click();
  await expect(page.locator(".actionNotice")).toHaveText(messages.run.budgetSaved);
  expect((await (await request.get(url)).json()).budget_minutes).toBe(50);
});

test("stale budget confirmation cannot re-extend after another exhaustion cycle", async ({ page, request, budgetWork: work }) => {
  const messages = getMessages("en");
  const url = `${api}/api/work-items/${work.id}`;
  await page.route(`${url}/events?*`, (route) => route.abort("failed"));
  await page.goto(`/en/work-items/${work.id}`);
  await page.getByRole("button", { name: messages.run.budgetOpen, exact: true }).click();
  expect((await request.post(`${url}/approvals`, { data: {
    kind: "budget", decision: "approve", expected_version: work.version, payload: { minutes: 15 },
  } })).status()).toBe(200);
  const latest = worker("exhaust", work.id);
  const dialog = page.getByRole("dialog");
  const confirm = dialog.getByRole("button", { name: messages.run.budgetConfirm, exact: true });
  const response = page.waitForResponse((result) => result.url() === `${url}/approvals` && result.request().method() === "POST");
  await confirm.click();
  expect((await response).status()).toBe(409);
  await expect(dialog.getByRole("alert")).toHaveText(messages.run.budgetConflict);
  await expect(confirm).toBeDisabled();
  await expect(dialog.locator(".budgetSummary dd").first()).toHaveText(`30 ${messages.run.minuteUnit}`);
  expect(await (await request.get(`${url}/audit-log`)).json()).toHaveLength(1);
  expect((await (await request.get(url)).json()).version).toBe(latest.version);
  await dialog.getByRole("button", { name: messages.run.budgetBack, exact: true }).click();
  const trigger = page.getByRole("button", { name: messages.run.budgetOpen, exact: true });
  await expect(trigger).toBeFocused();
  await trigger.click();
  await expect(dialog.locator(".budgetSummary dd").first()).toHaveText(`45 ${messages.run.minuteUnit}`);
  const fresh = page.waitForResponse((result) => result.url() === `${url}/approvals` && result.request().method() === "POST");
  await confirm.click();
  const accepted = await fresh;
  expect(accepted.status()).toBe(200);
  expect(accepted.request().postDataJSON().expected_version).toBe(latest.version);
  expect(await (await request.get(`${url}/audit-log`)).json()).toHaveLength(2);
});

test("lost budget success is reconciled without another extension or a false success notice", async ({ page, request, budgetWork: work }) => {
  const messages = getMessages("en");
  const url = `${api}/api/work-items/${work.id}`;
  await page.route(`${url}/events?*`, (route) => route.abort("failed"));
  await page.goto(`/en/work-items/${work.id}`);
  await page.getByRole("button", { name: messages.run.budgetOpen, exact: true }).click();
  await page.route(`${url}/approvals`, async (route) => {
    expect((await route.fetch()).status()).toBe(200);
    await route.abort("failed");
  });
  const dialog = page.getByRole("dialog");
  const confirm = dialog.getByRole("button", { name: messages.run.budgetConfirm, exact: true });
  await confirm.click();
  await expect(dialog.getByRole("alert")).toHaveText(messages.run.budgetNetworkError);
  await expect(confirm).toBeDisabled();
  await dialog.getByRole("button", { name: messages.run.budgetBack, exact: true }).click();
  await expect(page.locator(".runStatus")).toContainText(messages.status.implementing);
  await expect(page.locator(".runStatus")).toBeFocused();
  await expect(page.locator(".actionNotice")).toHaveCount(0);
  expect((await (await request.get(url)).json()).budget_minutes).toBe(90);
  expect(await (await request.get(`${url}/audit-log`)).json()).toHaveLength(1);
});

test("live budget version changes invalidate an open confirmation", async ({ page, request, budgetWork: work }) => {
  const messages = getMessages("en");
  const url = `${api}/api/work-items/${work.id}`;
  await page.goto(`/en/work-items/${work.id}`);
  await expect(page.locator(".connection-live")).toBeVisible();
  await page.getByRole("button", { name: messages.run.budgetOpen, exact: true }).click();
  expect((await request.post(`${url}/approvals`, { data: {
    kind: "budget", decision: "approve", expected_version: work.version, payload: { minutes: 15 },
  } })).status()).toBe(200);
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByRole("button", { name: messages.run.budgetConfirm, exact: true })).toBeDisabled();
  await expect(dialog.getByRole("alert")).toHaveText(messages.run.budgetConflict);
  await page.keyboard.press("Escape");
  await expect(dialog).not.toBeVisible();
  await expect(page.locator(".runStatus")).toBeFocused();
  expect(await (await request.get(`${url}/audit-log`)).json()).toHaveLength(1);
});
