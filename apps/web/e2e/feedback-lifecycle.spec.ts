import { getMessages } from "../i18n";
import { expect, test } from "./fixtures";

test("stale feedback is rejected by the real API and its draft remains copyable", async ({ page, request }) => {
  const response = await request.post("http://127.0.0.1:18100/api/work-items", { data: {
    title: "Feedback closed in another session", repository: "demo/feedback-lifecycle",
    requirement: "Preserve unsent feedback when a different approver starts delivery.",
  } });
  expect(response.status()).toBe(201);
  const work = await response.json();
  const url = `http://127.0.0.1:18100/api/work-items/${work.id}`;
  await expect.poll(async () => (await (await request.get(url)).json()).status).toBe("awaiting_approval");
  // Hold this browser's event stream offline; the API and Worker continue normally.
  await page.route(`${url}/events?*`, (route) => route.abort("failed"));
  await page.goto(`/en/work-items/${work.id}`);
  await expect(page.locator(".runStatus")).toContainText("Awaiting approval");
  await page.getByRole("textbox", { name: "Feedback", exact: true }).fill("Keep my unsent accessibility review.");
  const approval = await request.post(`${url}/approvals`, { data: { kind: "pull_request", decision: "approve", payload: {} } });
  expect(approval.ok()).toBe(true);
  await expect.poll(async () => (await (await request.get(url)).json()).status).toBe("completed");
  await expect(page.locator(".runStatus")).toContainText("Awaiting approval");
  const rejected = page.waitForResponse((item) => item.url() === `${url}/feedback` && item.request().method() === "POST");
  await page.getByRole("button", { name: /^Send to agent/ }).click();
  expect((await rejected).status()).toBe(409);
  await expect(page.locator(".formError")).toContainText("Your feedback was not sent");
  await expect(page.getByRole("heading", { name: "Feedback closed", exact: true })).toBeVisible();
  const draft = page.getByRole("textbox", { name: /^Unsent feedback/ });
  await expect(draft).toHaveValue("Keep my unsent accessibility review.");
  await expect(draft).toHaveAttribute("readonly", "");
  await draft.focus();
  await page.keyboard.press("ControlOrMeta+A");
  await expect(draft).toBeFocused();
  expect(await draft.evaluate((element: HTMLTextAreaElement) => element.selectionEnd - element.selectionStart)).toBe("Keep my unsent accessibility review.".length);
  await expect(page.getByRole("button", { name: /^Send to agent/ })).toHaveCount(0);
  await expect(page.locator(".actionNotice")).toHaveCount(0);
  const events: { event_type: string }[] = await (await request.get(`${url}/event-log`)).json();
  expect(events.filter((event) => event.event_type === "feedback.received")).toEqual([]);
});

test("live closure preserves focused input; all closed states have localized read-only controls", async ({ page, request }) => {
  const response = await request.post("http://127.0.0.1:18100/api/work-items", { data: {
    title: "Read-only state presentation", repository: "demo/feedback-states",
    requirement: "Check closed controls without losing a draft.",
  } });
  expect(response.status()).toBe(201);
  const created = await response.json();
  const url = `http://127.0.0.1:18100/api/work-items/${created.id}`;
  await expect.poll(async () => (await (await request.get(url)).json()).status).toBe("awaiting_approval");
  await page.goto(`/en/work-items/${created.id}`);
  const draft = page.locator('textarea[name="message"]');
  await draft.fill("Copy this draft before leaving.");
  const approval = await request.post(`${url}/approvals`, { data: { kind: "pull_request", decision: "approve", payload: {} } });
  expect(approval.ok()).toBe(true);
  await expect(page.locator(".runStatus")).toContainText("Completed");
  await expect(draft).toHaveAttribute("readonly", "");
  await expect(draft).toHaveValue("Copy this draft before leaving.");
  await expect(draft).toBeFocused();

  // Presentation fixtures only: the completed real work above is not mutated.
  const completed = await (await request.get(url)).json();
  await page.route(`${url}/artifacts`, (route) => route.fulfill({ json: [{
    id: "presentation-evidence", work_item_id: created.id, kind: "test-report",
    name: "verification.txt", content_type: "text/plain", size_bytes: 128,
    created_at: completed.updated_at,
  }] }));
  for (const locale of ["en", "ko"] as const) {
    const messages = getMessages(locale);
    await page.setViewportSize(locale === "ko" ? { width: 390, height: 844 } : { width: 1280, height: 900 });
    for (const status of ["committing", "pr_created", "completed", "failed", "cancelled"] as const) {
      await page.route(url, (route) => route.fulfill({ json: { ...completed, status, version: completed.version + 100 } }));
      await page.goto(`/${locale}/work-items/${created.id}`);
      await expect(page.locator(".runStatus")).toContainText(messages.status[status]);
      await expect(page.getByRole("heading", { name: messages.run.feedbackClosedTitle, exact: true })).toBeVisible();
      await expect(page.locator(".controlPanel textarea, .controlPanel button")).toHaveCount(0);
      await expect(page.locator(".artifactList a").first()).toBeVisible();
      await expect(page.locator(".artifactList a").first()).toHaveAttribute("href", `${url}/artifacts/presentation-evidence`);
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      await page.unroute(url);
    }
  }
});
