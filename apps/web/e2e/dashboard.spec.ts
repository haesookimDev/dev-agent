import { expect, test } from "./fixtures";

test("create, stream, feedback, re-verify and approve work using real services", async ({ page, request }) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/en");
  await expect(page.getByRole("heading", { name: "Work dashboard" })).toBeVisible();
  await page.getByRole("textbox", { name: "Repository", exact: true }).fill("demo/browser-test");
  await page.getByRole("textbox", { name: "Title", exact: true }).fill("Browser acceptance journey");
  await page.getByRole("textbox", { name: "Requirement", exact: true }).fill("Create work, verify it and accept the mock delivery.");
  await page.getByRole("button", { name: "Start development", exact: true }).click();
  await expect(page).toHaveURL(/\/en\/work-items\/[a-f0-9-]+$/);
  await expect(page.locator(".runStatus")).toContainText("Awaiting approval");
  await page.getByRole("textbox", { name: "Feedback", exact: true }).fill("Check the empty state before delivery.");
  const feedbackResponse = page.waitForResponse((response) => response.url().endsWith("/feedback") && response.request().method() === "POST");
  await page.getByRole("button", { name: /^Send to agent/ }).click();
  const acceptedFeedback = await feedbackResponse;
  expect(acceptedFeedback.status()).toBe(200);
  await expect(page.getByRole("heading", { name: "Check the empty state before delivery.", exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Mock revision ready for approval", exact: true })).toBeVisible();
  const approvalResponse = page.waitForResponse((response) => response.url().endsWith("/approvals") && response.request().method() === "POST");
  await page.getByRole("button", { name: /^Approve commit & PR/ }).click();
  const acceptedApproval = await approvalResponse;
  expect(acceptedApproval.status()).toBe(200);
  await expect(page.locator(".runStatus")).toContainText("Completed");
  await expect(page.getByRole("heading", { name: "Worker resources released", exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Feedback closed", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: /^Send to agent/ })).toHaveCount(0);
  await expect(page.getByRole("textbox", { name: "Feedback", exact: true })).toHaveCount(0);
  const workId = page.url().split("/").at(-1);
  const auditResponse = await request.get(`http://127.0.0.1:18100/api/work-items/${workId}/audit-log`);
  expect(auditResponse.status()).toBe(200);
  expect(auditResponse.headers()["cache-control"]).toBe("no-store");
  const audit = await auditResponse.json();
  expect(audit).toHaveLength(2);
  expect(audit[0]).toMatchObject({ action: "feedback.created", transport: "web", identity_provider: "development", effective_role: "administrator", required_role: "operator", request_id: acceptedFeedback.headers()["x-request-id"] });
  expect(audit[1]).toMatchObject({ action: "approval.decided", transport: "web", identity_provider: "development", required_role: "approver", request_id: acceptedApproval.headers()["x-request-id"], details: { kind: "pull_request", decision: "approve", work_status_before: "awaiting_approval", work_status_after: "committing", delivery_queued: false, delivery_bundle_sha256: null } });
  expect(JSON.stringify(audit)).not.toContain("Check the empty state before delivery.");
  const late = await request.post(`http://127.0.0.1:18100/api/work-items/${workId}/feedback`, { data: {
    message: "Must not accept feedback after delivery",
  } });
  expect(late.status()).toBe(409);
  expect(await (await request.get(`http://127.0.0.1:18100/api/work-items/${workId}/audit-log`)).json()).toEqual(audit);
  await page.reload();
  await expect(page.getByRole("heading", { name: "Feedback closed", exact: true })).toBeVisible();
  await page.getByRole("link", { name: "Switch to Korean" }).click();
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByRole("heading", { name: "피드백이 마감되었습니다", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: /^에이전트에게 전송/ })).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  expect(errors).toEqual([]);
});

test("search, status filters, language switch and narrow layout", async ({ page, request }) => {
  const response = await request.post("http://127.0.0.1:18100/api/work-items", { data: {
    title: "Filter and locale acceptance", repository: "demo/filter-test", requirement: "Verify independent filtering.",
  } });
  expect(response.status()).toBe(201);
  const work = await response.json();
  await expect.poll(async () => (await (await request.get(`http://127.0.0.1:18100/api/work-items/${work.id}`)).json()).status).toBe("awaiting_approval");
  await page.goto("/en");
  await page.getByRole("searchbox").fill("does-not-exist");
  await expect(page.getByRole("heading", { name: "No matching work" })).toBeVisible();
  await page.getByRole("button", { name: "Reset filters" }).click();
  await expect(page.getByRole("searchbox")).toHaveValue("");
  await page.getByRole("searchbox").fill("Filter and locale acceptance");
  await page.getByRole("combobox", { name: "Filter work status" }).selectOption("attention");
  await expect(page.locator(".workRow")).toHaveCount(1);
  await page.getByRole("link", { name: "Switch to Korean" }).click();
  await expect(page.getByRole("heading", { name: "작업 대시보드" })).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByRole("navigation", { name: "작업 공간" })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});
