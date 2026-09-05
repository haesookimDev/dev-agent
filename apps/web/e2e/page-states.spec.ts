import { expect, test } from "@playwright/test";

test("a missing work item provides a localized route back to the dashboard", async ({ page }) => {
  await page.goto("/ko/work-items/00000000-0000-0000-0000-000000000000");
  await expect(page.getByRole("heading", { name: "작업을 찾을 수 없습니다" })).toBeVisible();
  await page.getByRole("link", { name: "대시보드로 돌아가기", exact: true }).click();
  await expect(page.getByRole("heading", { name: "작업 대시보드" })).toBeVisible();
});

test("an invalid work address preserves the English locale", async ({ page }) => {
  await page.goto("/en/work-items/not-a-valid-id");
  await expect(page.getByRole("heading", { name: "Work item not found" })).toBeVisible();
  await page.getByRole("link", { name: "Back to dashboard", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Work dashboard" })).toBeVisible();
});
