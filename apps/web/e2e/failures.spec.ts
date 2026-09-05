import { expect, test } from "@playwright/test";

test("a failed create request preserves the form and permits retry", async ({ page }) => {
  await page.goto("/en");
  await page.route("http://127.0.0.1:18100/api/work-items", (route) => route.abort("failed"));
  await page.getByRole("textbox", { name: "Repository", exact: true }).fill("demo/retry");
  await page.getByRole("textbox", { name: "Title", exact: true }).fill("Retry after network failure");
  await page.getByRole("textbox", { name: "Requirement", exact: true }).fill("Keep the user's input when disconnected.");
  await page.getByRole("button", { name: "Start development", exact: true }).click();
  await expect(page.locator(".formError")).toBeVisible();
  await expect(page.getByRole("button", { name: "Start development", exact: true })).toBeEnabled();
  await expect(page.getByRole("textbox", { name: "Title", exact: true })).toHaveValue("Retry after network failure");
  await page.unroute("http://127.0.0.1:18100/api/work-items");
  await page.getByRole("button", { name: "Start development", exact: true }).click();
  await expect(page).toHaveURL(/\/en\/work-items\/[a-f0-9-]+$/);
});

test("feedback permission errors and approval network failures remain recoverable", async ({ page, request }) => {
  const response = await request.post("http://127.0.0.1:18100/api/work-items", { data: {
    title: "Recoverable work controls", repository: "demo/control-retry", requirement: "Verify failed mutations can be retried.",
  } });
  expect(response.status()).toBe(201);
  const work = await response.json();
  await page.goto(`/en/work-items/${work.id}`);
  await expect(page.locator(".runStatus")).toContainText("Awaiting approval");
  const feedbackUrl = `http://127.0.0.1:18100/api/work-items/${work.id}/feedback`;
  await page.route(feedbackUrl, (route) => route.fulfill({ status: 403, json: { detail: "operator role required" } }));
  await page.getByRole("textbox", { name: "Feedback", exact: true }).fill("Preserve this feedback.");
  await page.getByRole("button", { name: /^Send to agent/ }).click();
  await expect(page.locator(".formError")).toContainText("You do not have permission");
  await expect(page.getByRole("button", { name: /^Send to agent/ })).toBeEnabled();
  await expect(page.getByRole("textbox", { name: "Feedback", exact: true })).toHaveValue("Preserve this feedback.");
  await page.unroute(feedbackUrl);
  await page.getByRole("button", { name: /^Send to agent/ }).click();
  await expect(page.locator(".actionNotice")).toHaveText("Feedback sent.");
  await expect(page.getByRole("heading", { name: "Mock revision ready for approval", exact: true })).toBeVisible();
  const approvalUrl = `http://127.0.0.1:18100/api/work-items/${work.id}/approvals`;
  await page.route(approvalUrl, (route) => route.abort("failed"));
  await page.getByRole("button", { name: /^Approve commit & PR/ }).click();
  await expect(page.locator(".formError")).toContainText("Could not reach the server");
  await expect(page.getByRole("button", { name: /^Approve commit & PR/ })).toBeEnabled();
  await page.unroute(approvalUrl);
  await page.getByRole("button", { name: /^Approve commit & PR/ }).click();
  await expect(page.locator(".runStatus")).toContainText("Completed");
});
