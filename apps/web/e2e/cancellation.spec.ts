import type { APIRequestContext } from "@playwright/test";
import { getMessages } from "../i18n";
import { expect, test } from "./fixtures";

const api = "http://127.0.0.1:18100";

async function queuedWork(request: APIRequestContext) {
  const create = async (title: string) => {
    const response = await request.post(`${api}/api/work-items`, { data: {
      title, repository: "demo/cancellation", requirement: "Cancel an unwanted work item before Worker assignment.",
    } });
    expect(response.status()).toBe(201);
    return response.json();
  };
  // Keep the single real Mock Worker slot occupied; the next item stays genuinely queued.
  const blocker = await create("Keep the execution slot occupied during cancellation acceptance");
  await expect.poll(async () => (await (await request.get(`${api}/api/work-items/${blocker.id}`)).json()).status).toBe("awaiting_approval");
  return create("Unwanted queued work with a long descriptive title for safe cancellation confirmation");
}

for (const locale of ["en", "ko"] as const) {
  test(`${locale}: confirm queued cancellation, retain history and close feedback`, async ({ page, request }) => {
    const work = await queuedWork(request);
    const messages = getMessages(locale);
    const url = `${api}/api/work-items/${work.id}`;
    await page.setViewportSize(locale === "ko" ? { width: 390, height: 844 } : { width: 1280, height: 900 });
    await page.goto(`/${locale}/work-items/${work.id}`);
    await page.getByRole("textbox", { name: messages.run.feedback, exact: true }).fill("Keep this unsent review copyable.");
    const trigger = page.getByRole("button", { name: messages.run.cancelOpen, exact: true });
    await trigger.focus();
    await page.keyboard.press("Enter");
    const dialog = page.getByRole("dialog", { name: messages.run.cancelConfirmTitle });
    await expect(dialog).toBeVisible();
    await expect(dialog).toContainText(work.title);
    const back = dialog.getByRole("button", { name: messages.run.cancelBack, exact: true });
    await expect(back).toBeFocused();
    await page.keyboard.press("Escape");
    await expect(dialog).not.toBeVisible();
    await expect(trigger).toBeFocused();
    expect((await (await request.get(url)).json()).status).toBe("queued");
    expect(await (await request.get(`${url}/audit-log`)).json()).toEqual([]);
    await page.evaluate(() => {
      const dialog = document.querySelector("dialog")!;
      const close = dialog.close.bind(dialog);
      dialog.close = () => {
        close();
        // Observe focus before the deferred native close event. React may still
        // hold queued state there, then remove the trigger and lose keyboard focus.
        queueMicrotask(() => {
          document.documentElement.dataset.cancellationFocusRestored = String(
            document.activeElement === document.querySelector(".runStatus"),
          );
        });
      };
    });
    await trigger.click();
    await expect(back).toBeFocused();
    await page.keyboard.press("Tab");
    await expect(dialog.getByRole("button", { name: messages.run.cancelConfirm, exact: true })).toBeFocused();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    const result = page.waitForResponse((response) => response.url() === `${url}/cancel` && response.request().method() === "POST");
    await page.keyboard.press("Enter");
    expect((await result).status()).toBe(200);
    await expect(dialog).not.toBeVisible();
    await expect(page.locator(".runStatus")).toContainText(messages.status.cancelled);
    await expect(page.locator(".runStatus")).toBeFocused();
    await expect(page.locator("html")).toHaveAttribute("data-cancellation-focus-restored", "true");
    await expect(page.locator(".actionNotice")).toHaveText(messages.run.cancelledNotice);
    await expect(trigger).toHaveCount(0);
    await expect(page.locator(".controlPanel textarea")).toHaveValue("Keep this unsent review copyable.");
    await expect(page.locator(".controlPanel textarea")).toHaveAttribute("readonly", "");
    await expect(page.locator(".controlPanel button")).toHaveCount(0);
    const cancelled = await (await request.get(url)).json();
    expect(cancelled.version).toBe(work.version + 1);
    expect(cancelled.assigned_worker_id).toBeNull();
    const audit = await (await request.get(`${url}/audit-log`)).json();
    expect(audit).toHaveLength(1);
    expect(audit[0].action).toBe("work.cancelled");
    expect(audit[0].required_role).toBe("administrator");
    await page.reload();
    await expect(page.locator(".runStatus")).toContainText(messages.status.cancelled);
    await expect(page.getByRole("heading", { name: messages.run.feedbackClosedTitle, exact: true })).toBeVisible();
  });
}

test("failed cancellation keeps the confirmation usable and prevents duplicate submissions", async ({ page, request }) => {
  const work = await queuedWork(request);
  const url = `${api}/api/work-items/${work.id}`;
  const messages = getMessages("en");
  await page.goto(`/en/work-items/${work.id}`);
  await page.getByRole("button", { name: messages.run.cancelOpen, exact: true }).click();
  const dialog = page.getByRole("dialog");
  const confirm = dialog.getByRole("button", { name: messages.run.cancelConfirm, exact: true });
  let calls = 0;
  let release: () => void = () => {};
  const held = new Promise<void>((resolve) => { release = resolve; });
  await page.route(`${url}/cancel`, async (route) => {
    calls++;
    await held;
    await route.abort("failed");
  });
  try {
    await confirm.click();
    await expect(dialog.getByRole("button", { name: messages.run.cancelling, exact: true })).toBeDisabled();
    await page.keyboard.press("Enter");
    await page.keyboard.press("Escape");
    await expect(dialog).toBeVisible();
    expect(calls).toBe(1);
  } finally {
    release();
  }
  await expect(dialog.getByRole("alert")).toHaveText(messages.run.cancelNetworkError);
  await expect(confirm).toBeEnabled();
  await page.unroute(`${url}/cancel`);
  // Explicit UI denial fixture; OIDC role enforcement is covered by the API suite.
  await page.route(`${url}/cancel`, (route) => route.fulfill({ status: 403, json: { detail: "administrator role required" } }));
  await confirm.click();
  await expect(dialog.getByRole("alert")).toHaveText(messages.run.cancelPermissionError);
  expect((await (await request.get(url)).json()).status).toBe("queued");
  expect(await (await request.get(`${url}/audit-log`)).json()).toEqual([]);
  await page.unroute(`${url}/cancel`);
  await confirm.click();
  await expect(page.locator(".actionNotice")).toHaveText(messages.run.cancelledNotice);
});

test("a stale confirmation cannot cancel again after another administrator succeeds", async ({ page, request }) => {
  const work = await queuedWork(request);
  const url = `${api}/api/work-items/${work.id}`;
  const messages = getMessages("en");
  await page.route(`${url}/events?*`, (route) => route.abort("failed"));
  await page.goto(`/en/work-items/${work.id}`);
  await page.getByRole("button", { name: messages.run.cancelOpen, exact: true }).click();
  const other = await request.post(`${url}/cancel`, { data: { expected_version: work.version } });
  expect(other.status()).toBe(200);
  const dialog = page.getByRole("dialog");
  const confirm = dialog.getByRole("button", { name: messages.run.cancelConfirm, exact: true });
  const rejected = page.waitForResponse((response) => response.url() === `${url}/cancel` && response.request().method() === "POST");
  await confirm.click();
  expect((await rejected).status()).toBe(409);
  await expect(dialog.getByRole("alert")).toHaveText(messages.run.cancelConflict);
  await expect(confirm).toBeDisabled();
  await dialog.getByRole("button", { name: messages.run.cancelBack, exact: true }).click();
  await expect(page.locator(".runStatus")).toContainText(messages.status.cancelled);
  await expect(page.locator(".runStatus")).toBeFocused();
  await expect(page.locator(".actionNotice")).toHaveCount(0);
  expect(await (await request.get(`${url}/audit-log`)).json()).toHaveLength(1);
});

test("a lost success response is reconciled without offering another cancellation", async ({ page, request }) => {
  const work = await queuedWork(request);
  const url = `${api}/api/work-items/${work.id}`;
  const messages = getMessages("en");
  await page.route(`${url}/events?*`, (route) => route.abort("failed"));
  await page.goto(`/en/work-items/${work.id}`);
  await page.getByRole("button", { name: messages.run.cancelOpen, exact: true }).click();
  await page.route(`${url}/cancel`, async (route) => {
    const committed = await route.fetch();
    expect(committed.status()).toBe(200);
    await route.abort("failed");
  });
  const dialog = page.getByRole("dialog");
  const confirm = dialog.getByRole("button", { name: messages.run.cancelConfirm, exact: true });
  await confirm.click();
  await expect(dialog.getByRole("alert")).toHaveText(messages.run.cancelNetworkError);
  await expect(confirm).toBeDisabled();
  await dialog.getByRole("button", { name: messages.run.cancelBack, exact: true }).click();
  await expect(page.locator(".runStatus")).toContainText(messages.status.cancelled);
  await expect(page.locator(".actionNotice")).toHaveCount(0);
  expect(await (await request.get(`${url}/audit-log`)).json()).toHaveLength(1);
});

test("live state changes disable an open confirmation before submission", async ({ page, request }) => {
  const work = await queuedWork(request);
  const url = `${api}/api/work-items/${work.id}`;
  const messages = getMessages("en");
  await page.goto(`/en/work-items/${work.id}`);
  await expect(page.locator(".connection-live")).toBeVisible();
  await page.getByRole("button", { name: messages.run.cancelOpen, exact: true }).click();
  expect((await request.post(`${url}/cancel`, { data: { expected_version: work.version } })).status()).toBe(200);
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByRole("button", { name: messages.run.cancelConfirm, exact: true })).toBeDisabled();
  await expect(dialog.getByRole("alert")).toHaveText(messages.run.cancelConflict);
  await page.keyboard.press("Escape");
  await expect(dialog).not.toBeVisible();
  await expect(page.locator(".runStatus")).toBeFocused();
  expect(await (await request.get(`${url}/audit-log`)).json()).toHaveLength(1);
});
